"""
apply_handler.py — Self-contained auto-apply module
====================================================
Drop this file in the same folder as bot.py.

.env variables needed (add these):
    APPLY_EMAIL=youremail@gmail.com
    APPLY_PASSWORD=YourPassword@123

Install:
    pip install playwright
    playwright install chromium

4 minimal changes needed in bot.py — see bot_changes.txt
"""

import os
import re
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Callable, Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, CommandHandler, filters
from telegram.constants import ParseMode, ChatAction
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── New conversation states (10 and 11 — bot.py uses 0-9) ────────────────────
WAITING_APPLY_CONFIRM = 10
WAITING_APPLY_STUCK   = 11

# ── Per-user reply queues (for stuck field answers + email verify) ────────────
_reply_queues:  dict[int, asyncio.Queue] = {}
_cancel_events: dict[int, asyncio.Event] = {}

def get_reply_queue(user_id: int) -> asyncio.Queue:
    if user_id not in _reply_queues:
        _reply_queues[user_id] = asyncio.Queue()
    return _reply_queues[user_id]

def get_cancel_event(user_id: int) -> asyncio.Event:
    if user_id not in _cancel_events:
        _cancel_events[user_id] = asyncio.Event()
    return _cancel_events[user_id]

# ── All profile functions imported from profile_manager (single source of truth)
from profile_manager import (
    PROFILES_DIR,
    FIELD_MAP,
    load_profile,
    save_profile,
    learn_answer,
    get_field_value,
    merge_resume_into_profile,
    log_application,
    get_apply_stats,
    get_missing_fields,
    profile_completeness,
)

def get_best_resume(user_id: int, context_resume_path: str = "") -> str:
    """
    Pick resume to submit. Priority:
    1. context_resume_path if it exists on disk (explicitly set by session, e.g. ATS fix)
    2. Most recently modified PDF in user profile dir
    3. Fallback: context_resume_path even if not in profile dir
    """
    # Priority 1: explicit session path (set by last PDF generation step)
    if context_resume_path and Path(context_resume_path).exists():
        logger.info(f"  Resume: {Path(context_resume_path).name} (session/explicit)")
        return context_resume_path

    # Priority 2: latest modified PDF in user dir
    user_dir = PROFILES_DIR / str(user_id)
    all_pdfs = sorted(user_dir.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)
    if all_pdfs:
        logger.info(f"  Resume: {all_pdfs[0].name} (latest by mtime)")
        return str(all_pdfs[0])

    return ""


# ══════════════════════════════════════════════════════════════════════════════
# AUTO APPLY ENGINE (Playwright + Gemini HTML field detection)
# ══════════════════════════════════════════════════════════════════════════════

def detect_portal(url: str) -> str:
    u = url.lower()
    if "lever.co"        in u: return "lever"
    if "greenhouse.io"   in u: return "greenhouse"
    if "ashbyhq.com"     in u: return "ashby"
    if "linkedin.com"    in u: return "linkedin"
    if "workday.com"     in u: return "workday"
    if "myworkdayjobs"   in u: return "workday"
    if "smartrecruiters" in u: return "smartrecruiters"
    return "custom"

class ApplyResult:
    def __init__(self):
        self.status         = "pending"
        self.portal         = "unknown"
        self.company        = ""
        self.job_title      = ""
        self.fields_filled  = []
        self.fields_skipped = []
        self.fields_learned = []
        self.screenshot_path= ""
        self.error          = ""

async def _gemini_detect_fields(html: str, gemini_client, model: str) -> list:
    prompt = f"""You are analyzing the HTML of a job application form.
List every input field the candidate needs to fill.
Return ONLY a JSON array — no markdown.
Each item: {{"label": "field label", "name": "input name/id attr", "type": "text|email|phone|textarea|select|file|checkbox"}}
Ignore: hidden inputs, submit buttons, CSRF tokens.
HTML: {html}"""
    try:
        from google.genai import types as gt
        r = gemini_client.models.generate_content(
            model=model, contents=prompt,
            config=gt.GenerateContentConfig(temperature=0.1, response_mime_type="application/json")
        )
        return json.loads(r.text.strip().replace("```json","").replace("```","").strip())
    except Exception as e:
        logger.warning(f"Field detection failed: {e}")
        return []

async def _gemini_answer_field(question: str, profile: dict, gemini_client, model: str) -> Optional[str]:
    safe = {k: v for k, v in profile.items() if k != "password" and v}
    prompt = f"""You are filling a job application on behalf of a candidate.
Candidate profile: {json.dumps(safe, indent=2)}
Form field: "{question}"
Give a SHORT direct answer (1 sentence or less) from the candidate's profile.
If you cannot answer from the profile, reply with exactly: UNSURE"""
    try:
        from google.genai import types as gt
        r = gemini_client.models.generate_content(
            model=model, contents=prompt,
            config=gt.GenerateContentConfig(temperature=0.2)
        )
        answer = r.text.strip()
        return None if answer == "UNSURE" else answer
    except Exception as e:
        logger.warning(f"Field answer failed: {e}")
        return None

async def _safe_fill(page, selector: str, value: str, label: str = "") -> bool:
    try:
        el = page.locator(selector).first
        if await el.count() == 0 or not await el.is_visible(): return False
        await el.click()
        await el.fill("")
        await el.type(value, delay=25)
        logger.info(f"    Filled '{label or selector}'")
        return True
    except Exception: return False

async def _safe_upload(page, resume_path: str, result: ApplyResult) -> bool:
    for sel in ["input[type='file']", "input[accept*='pdf']", "#resume"]:
        try:
            if await page.locator(sel).count() > 0:
                await page.set_input_files(sel, resume_path)
                result.fields_filled.append("Resume")
                return True
        except Exception: continue
    result.fields_skipped.append("Resume")
    return False

# Known portal templates (Layer 1 — fast, reliable selectors)
SKIP_LABELS = {
    "name","email","phone","resume","cover letter","linkedin","portfolio",
    "github","website","first name","last name","full name","email address",
    "phone number","mobile number",
}

async def _fill_greenhouse(page, profile, resume_path, result):
    for sel, val, label in [
        ("#first_name",  profile.get("first_name",""),  "First name"),
        ("#last_name",   profile.get("last_name",""),   "Last name"),
        ("#email",       profile.get("email",""),        "Email"),
        ("#phone",       profile.get("phone",""),        "Phone"),
    ]:
        if val and await _safe_fill(page, sel, val, label):
            result.fields_filled.append(label)
    for sel in ["#job_application_location", "input[placeholder*='ocation' i]"]:
        if await _safe_fill(page, sel, profile.get("city",""), "Location"):
            result.fields_filled.append("Location"); break
    for sel in ["input[name*='linkedin' i]", "input[placeholder*='linkedin' i]"]:
        if await _safe_fill(page, sel, profile.get("linkedin",""), "LinkedIn"):
            result.fields_filled.append("LinkedIn"); break
    await _safe_upload(page, resume_path, result)

async def _fill_lever(page, profile, resume_path, result):
    for sel, val, label in [
        ("input[name='name']",           profile.get("full_name",""),       "Full name"),
        ("input[name='email']",          profile.get("email",""),           "Email"),
        ("input[name='phone']",          profile.get("phone",""),           "Phone"),
        ("input[name='org']",            profile.get("current_company",""), "Company"),
        ("input[name='urls[LinkedIn]']", profile.get("linkedin",""),        "LinkedIn"),
        ("input[name='urls[Portfolio]']",profile.get("portfolio",""),       "Portfolio"),
        ("input[name='urls[GitHub]']",   profile.get("github",""),          "GitHub"),
    ]:
        if val and await _safe_fill(page, sel, val, label):
            result.fields_filled.append(label)
    await _safe_upload(page, resume_path, result)

async def _fill_ashby(page, profile, resume_path, result):
    for sel, val, label in [
        ("input[name='name']",  profile.get("full_name",""), "Full name"),
        ("input[name='email']", profile.get("email",""),     "Email"),
        ("input[name='phone']", profile.get("phone",""),     "Phone"),
    ]:
        if val and await _safe_fill(page, sel, val, label):
            result.fields_filled.append(label)
    await _safe_upload(page, resume_path, result)

# AI-driven fill for unknown portals (Layer 2)
async def _fill_with_ai(page, profile, resume_path, result, user_id, gemini_client, model, on_stuck):
    try: html = await page.content()
    except Exception: return

    fields = await _gemini_detect_fields(html, gemini_client, model)
    logger.info(f"  AI detected {len(fields)} fields")

    for field in fields:
        label = field.get("label", "").strip()
        ftype = field.get("type", "text")
        fname = field.get("name", "")
        if not label: continue
        if ftype == "file":
            await _safe_upload(page, resume_path, result); continue
        if any(s in label.lower() for s in SKIP_LABELS): continue

        # Layer 1: profile lookup
        value = get_field_value(label, profile)
        # Layer 2: Gemini answer from profile context
        if not value:
            value = await _gemini_answer_field(label, profile, gemini_client, model)
        # Layer 3: ask user
        if not value and on_stuck:
            logger.info(f"  Asking user for: '{label}'")
            value = await on_stuck(label)
            if value:
                updated = learn_answer(user_id, label, value)
                profile.update(updated)
                result.fields_learned.append(f"{label}: {value}")

        if not value:
            result.fields_skipped.append(label); continue

        # Try to locate and fill the element
        filled = False
        if fname:
            for sel in [f"[name='{fname}']", f"#{fname}"]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        await el.fill(value); filled = True; break
                except Exception: continue
        if not filled:
            try:
                for_id = await page.locator(f"label:has-text('{label}')").first.get_attribute("for")
                if for_id:
                    await page.locator(f"#{for_id}").first.fill(value); filled = True
            except Exception: pass
        if not filled:
            for sel in [f"[aria-label='{label}']", f"[placeholder*='{label[:20]}']"]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        await el.fill(value); filled = True; break
                except Exception: continue

        if filled:
            result.fields_filled.append(label)
            logger.info(f"    Filled '{label}'")
        else:
            result.fields_skipped.append(label)

async def _try_submit(page, result) -> bool:
    for sel in [
        "button[type='submit']",
        "button:has-text('Submit Application')",
        "button:has-text('Submit')",
        "button:has-text('Apply Now')",
        "button:has-text('Apply')",
        "input[type='submit']",
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                await page.wait_for_load_state("networkidle", timeout=20000)
                return True
        except Exception: continue
    return False

async def run_apply(
    job_url:      str,
    resume_path:  str,
    user_id:      int,
    gemini_client,
    model:        str,
    pro_model:    str = "",
    on_stuck:     Callable = None,
    on_verify:    Callable = None,
    on_screenshot:Callable = None,
    on_notify:    Callable = None,
) -> ApplyResult:
    from playwright.async_api import async_playwright
    from linkedin_url_extractor import resolve_job_url

    profile = load_profile(user_id)
    result  = ApplyResult()
    Path("output").mkdir(exist_ok=True)

    # ── LinkedIn URL? Resolve to direct company portal URL first ──────────
    if "linkedin.com" in job_url.lower():
        if on_notify:
            await on_notify(
                "🔍 LinkedIn URL detected — finding the company's direct application page..."
            )

        async def on_need_linkedin_login():
            if on_notify:
                await on_notify(
                    "🔐 *LinkedIn session expired or not set up.*\n\n"
                    "Run this once on your machine to log in:\n"
                    "```\npython linkedin_url_extractor.py login\n```\n"
                    "A browser will open — log in manually, then cookies are saved automatically.\n\n"
                    "_Trying without login for now..._"
                )

        resolved = await resolve_job_url(
            job_url, gemini_client, model,
            on_need_login=on_need_linkedin_login
        )

        # Surface resolution errors immediately — don't fall through to Easy Apply with no session
        if resolved.get("error") and not resolved.get("apply_url") and not resolved.get("easy_apply"):
            result.status = "error"
            result.error  = resolved["error"]
            if on_notify:
                await on_notify(f"❌ Could not resolve job URL: {resolved['error']}")
            return result

        if resolved.get("apply_url"):
            if not result.job_title:
                result.job_title = resolved.get("job_title", "")
            if not result.company:
                result.company = resolved.get("company", "")
            if on_notify:
                await on_notify(
                    f"✅ Found direct application URL!\n\n"
                    f"💼 *{result.job_title}* at *{result.company}*\n"
                    f"🌐 Portal: *{resolved.get('portal','unknown')}*\n\n"
                    f"Starting form fill..."
                )
            job_url = resolved["apply_url"]
        else:
            result.job_title = resolved.get("job_title", "")
            result.company   = resolved.get("company", "")

            if resolved.get("easy_apply"):
                # ── Route to LinkedIn Easy Apply automation ────────────────
                from linkedin_easy_apply import run_easy_apply
                if on_notify:
                    await on_notify(
                        f"⚡ *LinkedIn Easy Apply detected!*\n\n"
                        f"💼 *{result.job_title}* at *{result.company}*\n\n"
                        f"Filling the Easy Apply form with your saved LinkedIn session..."
                    )
                easy_result = await run_easy_apply(
                    job_url       = job_url,
                    resume_path   = resume_path,
                    user_id       = user_id,
                    gemini_client = gemini_client,
                    model         = model,
                    pro_model     = pro_model,
                    on_stuck      = on_stuck,
                    on_screenshot = on_screenshot,
                    on_notify     = on_notify,
                )
                # Copy easy_result fields into main result
                result.status         = easy_result.status
                result.portal         = easy_result.portal
                result.fields_filled  = easy_result.fields_filled
                result.fields_skipped = easy_result.fields_skipped
                result.fields_learned = easy_result.fields_learned
                result.screenshot_path= easy_result.screenshot_path
                result.error          = easy_result.error
                # Log it
                log_application(user_id, {
                    "job_url": job_url, "job_title": result.job_title,
                    "company": result.company, "portal": "linkedin_easy_apply",
                    "status": result.status, "fields_filled": result.fields_filled,
                    "fields_skipped": result.fields_skipped,
                    "fields_learned": result.fields_learned, "error": result.error,
                })
                return result

            else:
                # Not Easy Apply — try opening the job page and detecting the external apply URL
                from linkedin_easy_apply import run_easy_apply
                if on_notify:
                    await on_notify(
                        f"🌐 *External apply detected*\n\n"
                        f"💼 *{result.job_title}* at *{result.company}*\n\n"
                        "Opening company application portal..."
                    )
                easy_result = await run_easy_apply(
                    job_url       = job_url,
                    resume_path   = resume_path,
                    user_id       = user_id,
                    gemini_client = gemini_client,
                    model         = model,
                    pro_model     = pro_model,
                    on_stuck      = on_stuck,
                    on_screenshot = on_screenshot,
                    on_notify     = on_notify,
                )
                result.status         = easy_result.status
                result.portal         = easy_result.portal or "external"
                result.fields_filled  = easy_result.fields_filled
                result.fields_skipped = easy_result.fields_skipped
                result.fields_learned = easy_result.fields_learned
                result.screenshot_path= easy_result.screenshot_path
                result.error          = easy_result.error
                log_application(user_id, {
                    "job_url": job_url, "job_title": result.job_title,
                    "company": result.company, "portal": result.portal,
                    "status": result.status, "fields_filled": result.fields_filled,
                    "fields_skipped": result.fields_skipped,
                    "fields_learned": result.fields_learned, "error": result.error,
                })
                return result

    result.portal = detect_portal(job_url)

    # ── Route to the unified, continuous engine for external/Workday portals ──
    # run_application drives apply_engine.converge_page across EVERY page (vision
    # audit + auto-submit) — replacing the old plan_page single-loop agent. It
    # logs the application itself, so no extra log_application here.
    from apply_orchestrator import run_application
    ext = await run_application(
        job_url=job_url, resume_path=resume_path, user_id=user_id,
        gemini_client=gemini_client, model=model, pro_model=pro_model,
        on_stuck=on_stuck, on_screenshot=on_screenshot, on_notify=on_notify,
    )
    result.status          = ext.status
    result.job_title       = ext.job_title or result.job_title
    result.company         = ext.company  or result.company
    result.fields_filled   = ext.fields_filled
    result.fields_skipped  = ext.fields_skipped
    result.fields_learned  = ext.fields_learned
    result.screenshot_path = ext.screenshot_path
    result.error           = ext.error
    return result


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def show_apply_prompt(message, context, job_url: str = ""):
    """
    Call this after resume PDF is sent to user.
    Shows apply buttons then returns WAITING_MAIN_CHOICE so
    subsequent messages (including voice for clarifications) work normally.
    """
    if job_url:
        context.user_data["pending_job_url"] = job_url

    has_url = bool(context.user_data.get("pending_job_url") or context.user_data.get("job_url"))

    if has_url:
        if not context.user_data.get("pending_job_url"):
            context.user_data["pending_job_url"] = context.user_data.get("job_url","")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 Yes, apply now!", callback_data="apply_now"),
                InlineKeyboardButton("❌ No thanks",       callback_data="apply_skip"),
            ]
        ])
        await message.reply_text(
            "🎯 *Resume is ready!*\n\nShall I auto-fill and submit the job application now?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
    else:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📋 Yes, I'll paste the job URL", callback_data="apply_want_url"),
                InlineKeyboardButton("❌ No thanks",                    callback_data="apply_skip"),
            ]
        ])
        await message.reply_text(
            "🎯 *Resume is ready!*\n\nWant me to auto-apply to a job? Paste the job URL.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
    return WAITING_APPLY_CONFIRM


async def handle_apply_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped 'Yes, apply now!'"""
    query   = update.callback_query
    user_id = update.effective_user.id
    await query.answer()

    job_url     = context.user_data.get("pending_job_url", "")
    resume_path = get_best_resume(update.effective_user.id, context.user_data.get("last_pdf_path") or context.user_data.get("resume_path",""))

    if not job_url:
        await query.edit_message_text("Please paste the job URL:")
        return WAITING_APPLY_CONFIRM

    if not resume_path or not Path(resume_path).exists():
        await query.edit_message_text("❌ Resume not found. Please /start and upload again.")
        return

    await query.edit_message_text(
        f"🤖 *Starting application...*\n\n"
        f"📄 Resume: `{Path(resume_path).name}`\n\n"
        "I'll fill the form and ask if I get stuck.\n"
        "You'll see a screenshot at each step.",
        parse_mode=ParseMode.MARKDOWN,
    )
    asyncio.create_task(_run_apply_task(update, context, user_id, job_url, resume_path))
    return WAITING_APPLY_STUCK


async def handle_apply_want_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User wants to apply but didn't have a URL — ask for it."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please paste the job application URL:")
    return WAITING_APPLY_CONFIRM


async def handle_apply_url_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User pasted a job URL while in WAITING_APPLY_CONFIRM."""
    text = update.message.text.strip()
    if not text.startswith("http"):
        await update.message.reply_text("That doesn't look like a URL. Please paste the full https:// link.")
        return WAITING_APPLY_CONFIRM

    context.user_data["pending_job_url"] = text
    resume_path = get_best_resume(update.effective_user.id, context.user_data.get("last_pdf_path") or context.user_data.get("resume_path",""))
    user_id = update.effective_user.id

    if not resume_path or not Path(resume_path).exists():
        await update.message.reply_text("❌ Resume not found. Please /start and upload again.")
        return

    await update.message.reply_text(
        "🤖 *Starting application...*\n\nI'll fill the form and ask if I get stuck.",
        parse_mode=ParseMode.MARKDOWN,
    )
    asyncio.create_task(_run_apply_task(update, context, user_id, text, resume_path))
    return WAITING_APPLY_STUCK


async def handle_apply_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("No problem! Your resume is saved. Use /start anytime.")
    return 1  # WAITING_MAIN_CHOICE


async def handle_apply_stuck_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any text while WAITING_APPLY_STUCK = user answering a stuck field."""
    user_id = update.effective_user.id
    text    = update.message.text.strip()
    await get_reply_queue(user_id).put(text)
    # Retry send on network timeout
    for attempt in range(3):
        try:
            await update.message.reply_text(
                f"✅ Got it: *{text}*\n_Continuing..._",
                parse_mode=ParseMode.MARKDOWN,
            )
            break
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(2)
            else:
                logger.warning(f"Could not send reply confirmation: {e}")
    return WAITING_APPLY_STUCK


async def handle_apply_submit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User confirmed 'Submit application!'"""
    query   = update.callback_query
    user_id = update.effective_user.id
    await query.answer()
    await query.edit_message_text("🚀 Submitting now...")

    job_url     = context.user_data.get("pending_job_url","")
    resume_path = get_best_resume(update.effective_user.id, context.user_data.get("last_pdf_path") or context.user_data.get("resume_path",""))
    chat_id     = update.effective_chat.id
    bot         = context.application.bot
    queue       = get_reply_queue(user_id)
    gemini_client = context.application.bot_data.get("gemini_client")
    model         = context.application.bot_data.get("model", "gemini-3.5-flash")
    pro_model     = context.application.bot_data.get("pro_model", model)

    async def on_stuck(q): # noqa
        await bot.send_message(chat_id=chat_id,
            text=f"❓ *Form is asking:*\n\n`{q}`\n\nPlease reply:",
            parse_mode=ParseMode.MARKDOWN)
        try: return await asyncio.wait_for(queue.get(), timeout=180)
        except asyncio.TimeoutError: return ""

    async def on_screenshot(path):
        try:
            with open(path,"rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f, caption="📸 Submitted!")
        except Exception: pass

    result = await run_apply(
        job_url=job_url, resume_path=resume_path, user_id=user_id,
        gemini_client=gemini_client, model=model, pro_model=pro_model,
        on_stuck=on_stuck, on_screenshot=on_screenshot,
    )
    await _send_result_message(bot, chat_id, result)
    return 1  # WAITING_MAIN_CHOICE


async def handle_apply_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel button — stops running apply task and clears queue."""
    query   = update.callback_query
    user_id = update.effective_user.id
    await query.answer()

    # Signal the running task to stop
    cancel_event = _cancel_events.get(user_id)
    if cancel_event:
        cancel_event.set()
        logger.info(f"Cancel event set for user {user_id}")

    # Drain the reply queue so stuck callbacks unblock
    queue = get_reply_queue(user_id)
    while not queue.empty():
        try: queue.get_nowait()
        except Exception: pass
    # Put a cancel signal in case task is waiting for user input
    await queue.put("skip")

    await query.edit_message_text(
        "❌ Application cancelled.\n\nUse /start to go back to the menu."
    )
    return 1  # WAITING_MAIN_CHOICE


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/report — show application stats."""
    user_id = update.effective_user.id
    stats   = get_apply_stats(user_id)
    if stats["total"] == 0:
        await update.message.reply_text("No applications yet. Use /start to get going!")
        return
    portals = "\n".join(f"  • {p}: {c}" for p, c in stats.get("portals",{}).items())
    await update.message.reply_text(
        f"📊 *Application Report*\n\n"
        f"Total      : *{stats['total']}*\n"
        f"Submitted  : *{stats['submitted']}*\n"
        f"Failed     : *{stats['failed']}*\n\n"
        f"*By portal:*\n{portals}\n\n"
        f"Last applied : {stats.get('last_applied','—')}\n"
        f"Last company : {stats.get('last_company','—')}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _run_apply_task(update, context, user_id, job_url, resume_path):
    """Runs as asyncio.create_task — keeps bot responsive during apply."""
    bot     = context.application.bot
    chat_id = update.effective_chat.id
    queue   = get_reply_queue(user_id)
    gemini_client = context.application.bot_data.get("gemini_client")
    model         = context.application.bot_data.get("model", "gemini-3.5-flash")
    pro_model     = context.application.bot_data.get("pro_model", model)

    # Reset cancel event for this new apply session
    cancel_event = get_cancel_event(user_id)
    cancel_event.clear()

    # ── Auto-refresh profile from latest resume ───────────────────────────
    # Extract text from PDF and update profile fields — always uses latest resume
    if resume_path and Path(resume_path).exists():
        try:
            import fitz  # PyMuPDF
            doc  = fitz.open(resume_path)
            text = "\n".join(p.get_text() for p in doc)
            doc.close()
            if text.strip():
                merge_resume_into_profile(user_id, text, gemini_client, model)
                logger.info(f"  Profile refreshed from {Path(resume_path).name}")
        except Exception as e:
            logger.debug(f"  Profile refresh failed: {e}")

    async def on_stuck(question: str) -> str:
        # Check cancelled before waiting
        if cancel_event.is_set(): return "skip"
        await bot.send_message(
            chat_id=chat_id,
            text=(f"❓ *The form is asking:*\n\n`{question}`\n\n"
                  "Reply with your answer — I'll fill it and remember it for next time.\n"
                  "_Or tap Cancel below to stop._"),
            parse_mode=ParseMode.MARKDOWN,
        )
        try:
            # Race between user reply and cancel event
            reply_task  = asyncio.create_task(queue.get())
            cancel_task = asyncio.create_task(cancel_event.wait())
            done, pending = await asyncio.wait(
                [reply_task, cancel_task],
                timeout=180,
                return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending: t.cancel()
            if cancel_event.is_set(): return "skip"
            if reply_task in done: return reply_task.result()
            await bot.send_message(chat_id=chat_id, text="⏰ No reply — skipping that field.")
            return ""
        except Exception:
            return ""

    async def on_verify(message: str) -> bool:
        await bot.send_message(chat_id=chat_id,
            text=f"📧 *Email verification needed*\n\n{message}", parse_mode=ParseMode.MARKDOWN)
        try:
            reply = await asyncio.wait_for(queue.get(), timeout=300)
            return "done" in (reply or "").lower()
        except asyncio.TimeoutError: return False

    async def on_screenshot(path: str):
        try:
            with open(path,"rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f,
                                     caption="📸 Here's the filled form — review before submitting")
        except Exception as e:
            logger.warning(f"Screenshot send failed: {e}")

    async def on_notify(message: str):
        """Send a plain status update to user during apply."""
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=ParseMode.MARKDOWN,
        )

    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        result = await run_apply(
            job_url=job_url, resume_path=resume_path, user_id=user_id,
            gemini_client=gemini_client, model=model, pro_model=pro_model,
            on_stuck=on_stuck, on_verify=on_verify, on_screenshot=on_screenshot,
            on_notify=on_notify,
        )

        if result.status == "success":
            # Already submitted (LinkedIn Easy Apply submits directly)
            filled_text = "\n".join(f"  ✅ {f}" for f in result.fields_filled[:10])
            learned_text = (
                "\n\n💾 *Saved to your profile:*\n" +
                "\n".join(f"  • {l}" for l in result.fields_learned)
            ) if result.fields_learned else ""
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🎉 *Application Submitted!*\n\n"
                    f"*Fields filled:*\n{filled_text}"
                    f"{learned_text}"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )


        elif result.status == "failed":
            err = (result.error or "❌ Application failed. Please try again.")
            # Strip markdown chars from error to avoid parse errors
            err = err.replace("`", "").replace("*", "").replace("_", "")
            await bot.send_message(chat_id=chat_id, text=err)

    except Exception as e:
        logger.error(f"Apply task error user {user_id}: {e}")
        await bot.send_message(chat_id=chat_id,
            text=f"❌ Something went wrong: {str(e)[:200].replace(chr(96), '').replace(chr(42), '')}")


async def _send_result_message(bot, chat_id: int, result: ApplyResult):
    learned = (
        "\n\n💾 *Saved to your profile:*\n" +
        "\n".join(f"  • {l}" for l in result.fields_learned)
    ) if result.fields_learned else ""

    if result.status == "success":
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🎉 *Application submitted!*\n\n"
                f"🏢 Company : {result.company or 'N/A'}\n"
                f"💼 Role    : {result.job_title or 'N/A'}\n"
                f"🌐 Portal  : {result.portal}\n"
                f"✅ Fields  : {len(result.fields_filled)} filled"
                f"{learned}\n\n_Good luck! 🤞_"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        # Show error message directly — it already has formatting and instructions
        await bot.send_message(
            chat_id=chat_id,
            text=result.error or "❌ Application failed. Please try again.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ══════════════════════════════════════════════════════════════════════════════
# get_apply_handlers() — call this in bot.py main() to register everything
# ══════════════════════════════════════════════════════════════════════════════

def get_apply_handlers():
    """
    Returns (conv_states_dict, global_handlers_list, commands_list).
    Use in bot.py main() — see bot_changes.txt for exact insertion points.
    """
    conv_states = {
        WAITING_APPLY_CONFIRM: [
            CallbackQueryHandler(handle_apply_now,      pattern="^apply_now$"),
            CallbackQueryHandler(handle_apply_want_url, pattern="^apply_want_url$"),
            CallbackQueryHandler(handle_apply_skip,     pattern="^apply_skip$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_apply_url_input),
        ],
        WAITING_APPLY_STUCK: [
            MessageHandler(filters.VOICE, handle_apply_stuck_reply),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_apply_stuck_reply),
        ],
    }
    global_handlers = [
        CallbackQueryHandler(handle_apply_submit_confirm, pattern="^apply_submit_confirm$"),
        CallbackQueryHandler(handle_apply_cancel,         pattern="^apply_cancel$"),
        CallbackQueryHandler(handle_apply_now,            pattern="^apply_now$"),
        CallbackQueryHandler(handle_apply_skip,           pattern="^apply_skip$"),
    ]
    commands = [
        CommandHandler("report", cmd_report),
    ]
    return conv_states, global_handlers, commands
