"""
bot.py — Telegram Resume Optimization Bot
==========================================
Install: uv pip install python-telegram-bot google-genai python-dotenv
         selenium webdriver-manager fpdf2 pdfplumber

.env:
  TELEGRAM_BOT_TOKEN=...
  GEMINI_API_KEY=...

Run: python bot.py
"""

import os
import json
import shutil
import asyncio
import logging
import tempfile
from pathlib import Path
from datetime import datetime

from telegram import (
    Update, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    ContextTypes, filters, PicklePersistence,
)
from telegram.constants import ParseMode, ChatAction

from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv

from scraper import scrape_url_content
from job_fetcher import fetch_jobs, COMPANY_GROUPS
from parser import parse_resume_with_gemini
from resume_pdf import generate_resume_pdf
from ats_checker import check_ats_score
from prompts import (
    build_analysis_prompt,
    build_rewrite_with_context_prompt,
    build_update_rewrite_prompt,
    build_low_score_guidance_prompt,
    build_formatting_fix_prompt,
    build_format_verify_prompt,
    REWRITE_THRESHOLD,
)
from apply_handler import (
    show_apply_prompt, get_apply_handlers,
    merge_resume_into_profile, WAITING_APPLY_CONFIRM, WAITING_APPLY_STUCK,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not found in .env")
if not GEMINI_KEY:
    raise ValueError("GEMINI_API_KEY not found in .env")

gemini_client = genai.Client(api_key=GEMINI_KEY)

PRO_MODEL   = "gemini-3.5-flash"  # resume rewrite
FLASH_MODEL = "gemini-3.5-flash"  # match analysis
FLASH_PRO   = "gemini-3.5-flash"  # format + verify passes
LITE_MODEL  = "gemini-3.5-flash"  # simple tasks: questions, ranking, roadmap, merge, ATS

TEMP_DIR     = Path("temp_resumes")
OUTPUT_DIR   = Path("output")
PROFILES_DIR = Path("user_profiles")
for d in (TEMP_DIR, OUTPUT_DIR, PROFILES_DIR):
    d.mkdir(exist_ok=True)

def cleanup_output_dir(keep: int = 50):
    pdfs = sorted(OUTPUT_DIR.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)
    for old_pdf in pdfs[keep:]:
        try:
            old_pdf.unlink()
        except Exception:
            pass

ACCESS_KEY   = os.getenv("ACCESS_KEY", "")
FREE_LIMIT   = 5

# ── User profile storage ──────────────────────────────────────────────────────

def get_user_dir(user_id: int) -> Path:
    d = PROFILES_DIR / str(user_id)
    d.mkdir(exist_ok=True)
    return d

def save_resume_for_user(user_id: int, pdf_path: str, md_text: str = "") -> Path:
    user_dir = get_user_dir(user_id)
    version  = len(sorted(user_dir.glob("resume_v*.pdf"))) + 1
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved    = user_dir / f"resume_v{version}_{ts}.pdf"
    shutil.copy2(pdf_path, saved)
    if md_text:
        saved.with_suffix(".md").write_text(md_text, encoding="utf-8")
    for old in sorted(user_dir.glob("resume_v*.pdf"))[:-5]:
        old.unlink(missing_ok=True)
        old.with_suffix(".md").unlink(missing_ok=True)
    return saved

def get_latest_resume(user_id: int) -> tuple:
    pdfs = sorted(get_user_dir(user_id).glob("resume_v*.pdf"))
    if not pdfs:
        return None, None
    p = pdfs[-1]
    m = p.with_suffix(".md")
    return p, (m if m.exists() else None)

def list_user_resumes(user_id: int) -> list:
    return sorted(get_user_dir(user_id).glob("resume_v*.pdf"), reverse=True)

# ── Access control ────────────────────────────────────────────────────────────

def get_access_file(user_id: int) -> Path:
    return get_user_dir(user_id) / "access.json"

def load_access(user_id: int) -> dict:
    f = get_access_file(user_id)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"uses": 0, "unlocked": False}

def save_access(user_id: int, data: dict):
    get_access_file(user_id).write_text(json.dumps(data), encoding="utf-8")

def increment_use(user_id: int) -> dict:
    data = load_access(user_id)
    if not data["unlocked"]:
        data["uses"] = data.get("uses", 0) + 1
    save_access(user_id, data)
    return data

def is_allowed(user_id: int) -> bool:
    data = load_access(user_id)
    return data.get("unlocked", False) or data.get("uses", 0) < FREE_LIMIT

def remaining_uses(user_id: int) -> int:
    data = load_access(user_id)
    if data.get("unlocked"):
        return 999
    return max(0, FREE_LIMIT - data.get("uses", 0))

async def handle_access_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    user_id = update.effective_user.id
    if not ACCESS_KEY:
        await update.message.reply_text("No access key configured. Contact the bot owner.")
        return
    if text == ACCESS_KEY:
        data = load_access(user_id)
        data["unlocked"] = True
        save_access(user_id, data)
        await update.message.reply_text(
            "Access unlocked! You now have unlimited resume generations. Use /start to continue.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "Invalid key. Please check and try again, or contact the bot owner.",
        )

async def _check_limit_and_gate(message, user_id: int) -> bool:
    if is_allowed(user_id):
        return True
    await message.reply_text(
        "Free limit reached! You have used your " + str(FREE_LIMIT) + " free resume generations."
        " To continue, type your access key in chat. Contact the bot owner if you need one.",
    )
    return False

def resolve_resume_path(context, user_id: int) -> str | None:
    resume_path = context.user_data.get("resume_path", "")
    if resume_path and Path(resume_path).exists():
        return resume_path
    saved_pdf, _ = get_latest_resume(user_id)
    if saved_pdf and saved_pdf.exists():
        context.user_data["resume_path"] = str(saved_pdf)
        logger.info(f"resume_path missing, fell back to profile: {saved_pdf}")
        return str(saved_pdf)
    return None

# ── Conversation states ───────────────────────────────────────────────────────
(
    WAITING_RESUME,
    WAITING_MAIN_CHOICE,
    WAITING_RESUME_UPDATE,
    WAITING_JOB_PREFS,
    WAITING_JOB_TIME,
    WAITING_JOB_URL,
    WAITING_CLARIFICATION,
    WAITING_SKILLS_INPUT,
    WAITING_ATS_CONFIRM,
    WAITING_UPDATE_CONFIRM,
) = range(10)

# ── LLM helpers ───────────────────────────────────────────────────────────────

def call_llm(prompt: str, model_name: str = FLASH_MODEL) -> dict:
    import time
    FALLBACKS = [model_name, "gemini-3.5-flash", "gemini-3.5-flash"]
    last_err  = None
    for model in FALLBACKS:
        for attempt in range(3):
            try:
                r = gemini_client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.3,
                        response_mime_type="application/json",
                    )
                )
                raw = r.text.strip().replace("```json","").replace("```","").strip()
                return json.loads(raw)
            except Exception as e:
                err = str(e)
                if "503" in err or "429" in err or "overloaded" in err.lower():
                    wait = 3 * (attempt + 1)
                    logger.warning(f"{model} overloaded, retry in {wait}s")
                    last_err = e
                    time.sleep(wait)
                else:
                    raise
    raise RuntimeError(f"All models failed: {last_err}")

def call_llm_text(prompt: str, model_name: str = FLASH_MODEL) -> str:
    r = gemini_client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.3)
    )
    return r.text.strip()




async def chain_rewrite(resume_md: str, original_resume: str = "") -> tuple[str, list]:
    """
    Sequential chain — output of each step feeds into the next.
    original_resume: the pre-rewrite text, used to extract protected names reliably.
    """
    import re as _re, asyncio

    # Extract protected names from ORIGINAL resume (before any LLM touched it)
    # This ensures "IIT Hyderabad" is in the list even if Step 1 truncated it
    source = original_resume if original_resume else resume_md
    protected = _re.findall(r'\*\*([^|*\n]+?)\s*\|', source)
    # Also find names without bold markers (plain text education entries)
    protected += _re.findall(r'(?:^|\n)([A-Z][^\n|*]{3,50})\s*\|', source)
    protected = list(set(n.strip() for n in protected if len(n.strip()) > 2))
    all_changes = []

    # Step 2+3 — Combined format + verify pass (single API call, halves latency)
    try:
        loop = asyncio.get_event_loop()
        fv = await loop.run_in_executor(
            None, lambda: call_llm(build_format_verify_prompt(resume_md, protected), PRO_MODEL)
        )
        for patch in fv.get("patches", []):
            find, replace = patch.get("find", ""), patch.get("replace", "")
            if find and find in resume_md:
                resume_md = resume_md.replace(find, replace, 1)
        all_changes += fv.get("fixes", [])
        logger.info(f"Format+verify pass: {len(fv.get('patches', []))} patches, fixes: {fv.get('fixes', [])}")
    except Exception as e:
        logger.warning(f"Format+verify pass failed: {e}")

    return resume_md, all_changes

# ── Voice transcription ───────────────────────────────────────────────────────

async def transcribe_voice(path: str) -> str:
    import time as _t
    try:
        up = gemini_client.files.upload(
            file=path,
            config=genai_types.UploadFileConfig(mime_type="audio/ogg")
        )
        for _ in range(15):
            info = gemini_client.files.get(name=up.name)
            if "ACTIVE" in str(info.state).upper():
                break
            _t.sleep(1)
        r = gemini_client.models.generate_content(
            model=FLASH_MODEL,
            contents=[
                genai_types.Part.from_uri(file_uri=up.uri, mime_type="audio/ogg"),
                "Transcribe this voice note accurately. Return only the transcribed text."
            ],
            config=genai_types.GenerateContentConfig(temperature=0.0)
        )
        try:
            gemini_client.files.delete(name=up.name)
        except Exception:
            pass
        return r.text.strip()
    except Exception as e:
        return f"[Transcription failed: {e}]"

# ── Prompts ───────────────────────────────────────────────────────────────────

def build_clarifying_questions_prompt(jd: str, resume: str, analysis: dict) -> str:
    missing_crit = "\n".join(f"- {s}" for s in analysis.get("missing_critical", []))
    missing_pref = "\n".join(f"- {s}" for s in analysis.get("missing_preferred", []))
    keywords     = ", ".join(analysis.get("ats_keywords_to_add", []))
    return f"""
You are a sharp resume coach. Ask 2-3 targeted questions to uncover skills the candidate
HAS but forgot to mention. Do NOT ask about things already on the resume. Max 3 questions.

## JOB DESCRIPTION:
{jd}

## RESUME:
{resume}

## GAPS:
Critical missing: {missing_crit}
Preferred missing: {missing_pref}
ATS keywords not in resume: {keywords}

Return JSON only:
{{
  "should_ask": true/false,
  "questions": [{{"id":"q1","question":"<short>","context":"<gap addressed>"}}]
}}
Rules: should_ask=false if resume covers everything. Max 3 questions, 1 sentence each.
"""

def fmt_analysis(analysis: dict, score: int, match_level: str) -> str:
    bar   = "🟩" * int(score/5) + "⬜" * (20 - int(score/5))
    text  = f"📊 *Match Analysis*\n\nScore: *{score}/100* — {match_level}\n{bar}\n\n"
    text += f"_{analysis.get('score_rationale','')}_\n\n"
    if analysis.get("matched_skills"):
        text += "✅ *Matched*\n" + "\n".join(f"  • {s}" for s in analysis["matched_skills"][:6]) + "\n\n"
    if analysis.get("missing_critical"):
        text += "❌ *Critical Gaps*\n" + "\n".join(f"  • {s}" for s in analysis["missing_critical"][:4]) + "\n\n"
    if analysis.get("missing_preferred"):
        text += "⚠️ *Preferred Gaps*\n" + "\n".join(f"  • {s}" for s in analysis["missing_preferred"][:3]) + "\n\n"
    if analysis.get("ats_keywords_to_add"):
        text += "🎯 *ATS Keywords*\n  " + ", ".join(analysis["ats_keywords_to_add"][:6]) + "\n"
    return text

def fmt_roadmap(guidance: dict) -> str:
    text  = "🗺 *Improvement Roadmap*\n\n"
    text += f"_{guidance.get('honest_assessment','')}_\n\n"
    text += f"⏱ Time to ready: *{guidance.get('estimated_time_to_ready','N/A')}*\n\n"
    for item in guidance.get("skill_gap_roadmap",[])[:4]:
        text += f"*{item.get('skill')}* ({item.get('timeframe')})\n  → {item.get('how_to_learn')}\n\n"
    if guidance.get("quick_wins"):
        text += "⚡ *Quick Wins*\n" + "\n".join(f"  • {w}" for w in guidance["quick_wins"][:3]) + "\n"
    if guidance.get("encouragement"):
        text += f"\n💪 _{guidance['encouragement']}_"
    return text

def build_resume_analysis_prompt(resume_text: str) -> str:
    return f"""
You are a senior resume coach. Analyze this resume and identify what is missing or weak.

## RESUME:
{resume_text}

Return JSON only:
{{
  "overall_quality": "<Poor|Fair|Good|Strong>",
  "missing_sections": ["<section name>", ...],
  "weak_sections": [
    {{"section": "<name>", "issue": "<what is weak>", "example": "<what good looks like>"}}
  ],
  "missing_info": ["<specific info that should be added>", ...],
  "summary": "<2 sentence plain English assessment>"
}}
"""

def build_resume_update_prompt(existing_resume: str, user_update: str) -> str:
    from datetime import date
    today_str = date.today().strftime("%b %Y")
    return f"""
You are an expert resume editor. Your ONLY job is to merge new information
into an existing resume. Do NOT rewrite style or tone — just add/update facts.

## EXISTING RESUME:
{existing_resume}

## NEW INFORMATION FROM CANDIDATE:
{user_update}

## TODAY'S DATE: {today_str}

## YOUR TASK:

1. EXPERIENCE CALCULATION — do this first, carefully:
   - Find the earliest career start date across ALL roles
   - Calculate total experience from that date to TODAY ({today_str})
   - Express as "X years Y months" — NEVER round to whole years
   - If candidate explicitly states their experience (e.g. "5.6 years"),
     convert it properly: 5.6 years = 5 years 7 months
   - Update the Summary section to reflect the correct total
   - Store the calculated value in the "total_experience" field of your JSON

2. MERGING CONTENT:
   - Add any new jobs, projects, skills, certifications mentioned
   - Update dates, titles, or bullet points if the candidate described changes
   - Keep ALL existing content that was not mentioned as changing
   - Do NOT invent anything not in the existing resume or the update text

3. NEW CONTENT QUALITY — apply basic REUSE:
   - Start bullets with action verbs (Automated, Built, Reduced, Led, Delivered)
   - Add numbers where clearly implied by the candidate's description
   - Use [Action] + [What] + [Result] format
   - Keep bullets to 1-2 lines, no filler words

## STRICT FORMAT RULES:
- Resume starts with: # Full Name
- Contact line: email | phone | linkedin | city
- Section headers: ## SECTION NAME
- Experience: **Company | Role | Mon YYYY - Mon YYYY** (ONE line, no location)
- Bullets: MUST start with "- " (hyphen space). NEVER "* " or "•"
- NO trailing asterisks anywhere
- Skills: **Category:** skill1, skill2, skill3
- Dates: "Mon YYYY - Mon YYYY" only

Return JSON only, no markdown fences:
{{
  "optimized_resume_text": "<complete resume in exact markdown format above>",
  "changes_made": ["<specific change 1>", "<specific change 2>", "<specific change 3>"],
  "total_experience": "<e.g. 3 years 1 month>"
}}
"""

def build_job_ranking_prompt(jobs: list, resume_text: str) -> str:
    jobs_text = "\n\n".join(
        f"JOB {i+1}:\nTitle: {j.get('title','')}\nCompany: {j.get('company','')}\n"
        f"URL: {j.get('url','')}\nJob ID: {j.get('job_id','')}"
        for i, j in enumerate(jobs)
    )
    return f"""
You are a technical recruiter. Given a candidate's resume and a list of job postings,
score each job for fit with the candidate's profile.

## CANDIDATE RESUME:
{resume_text}

## JOB POSTINGS:
{jobs_text}

Return JSON only:
{{
  "ranked_jobs": [
    {{
      "job_id": "<job_id from input>",
      "title": "<job title>",
      "company": "<company>",
      "url": "<url>",
      "match_score": <integer 0-100>,
      "reason": "<10 words max: what matches, what's missing>"
    }}
  ]
}}

Sort by match_score descending. Include ALL jobs.
"""

# ── ATS auto-fix ──────────────────────────────────────────────────────────────

async def _run_ats_and_fix(message, context, resume_md: str, pdf_path: str):
    jd = context.user_data.get("job_description", "")
    await message.reply_text("🔍 Running ATS analysis...")
    ats = None
    try:
        ats = check_ats_score(pdf_path, jd)
        await message.reply_text(ats.format_for_telegram(), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await message.reply_text(f"ATS check error: {e}")
        return
    if not ats or ats.overall_score >= 88:
        return
    formatting_issues, skill_gaps = ats.classify_issues()
    if formatting_issues and ats.has_fixable_issues():
        await message.reply_text(f"Found {len(formatting_issues)} formatting issue(s) — auto-fixing...")
        try:
            raw_fix = call_llm_text(build_formatting_fix_prompt(resume_md, jd, formatting_issues, ats.overall_score), LITE_MODEL)
            fix = {}
            fixed = ""
            if "---RESUME---" in raw_fix:
                meta_part, resume_part = raw_fix.split("---RESUME---", 1)
                fixed = resume_part.strip()
                try:
                    fix = json.loads(meta_part.strip())
                except Exception:
                    fix = {"fixes_applied": [], "expected_score_improvement": 5}
            if fixed:
                ts2      = datetime.now().strftime("%Y%m%d_%H%M%S")
                user_id  = message.chat.id
                user_dir = get_user_dir(user_id)
                p2       = str(user_dir / f"resume_ats_{ts2}.pdf")
                generate_resume_pdf(fixed, p2)
                context.user_data["last_pdf_path"] = p2
                fixes_text = "\n".join(f"  - {f}" for f in fix.get("fixes_applied",[])[:5])
                await message.reply_text(
                    f"*Improved version ready!*\n\n{fixes_text}\n\n"
                    f"Expected: ~{ats.overall_score + fix.get('expected_score_improvement',0)}/100",
                    parse_mode=ParseMode.MARKDOWN,
                )
                with open(p2,"rb") as f:
                    await message.reply_document(document=f, filename=f"resume_ats_{ts2}.pdf",
                                                 caption="Improved ATS resume (v2)")
        except Exception as e:
            await message.reply_text(f"Auto-fix failed: {e}")
    if skill_gaps:
        gaps  = "\n".join(f"  - {g}" for g in skill_gaps[:6])
        kws   = "\n".join(f"  - {k}" for k in ats.keyword_misses[:6])
        parts = ["*Skill gaps — need real experience:*\n", gaps]
        if ats.keyword_misses:
            parts.append(f"\n*Missing JD keywords:*\n{kws}")
        parts.append("\n_These cannot be fabricated._")
        await message.reply_text("\n".join(parts), parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════════════════════════════
# /start
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    first_name = update.effective_user.first_name or "there"
    user_id    = update.effective_user.id
    saved_pdf, saved_md = get_latest_resume(user_id)

    if saved_pdf:
        context.user_data["resume_path"] = str(saved_pdf)
        context.user_data["resume_name"] = saved_pdf.name
        if saved_md:
            try:
                context.user_data["resume_text"] = saved_md.read_text(encoding="utf-8")
            except Exception:
                pass
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Use this resume",      callback_data="resume_use_saved")],
            [InlineKeyboardButton("📥 View saved resume",    callback_data="resume_view_saved")],
            [InlineKeyboardButton("📤 Upload a new resume",  callback_data="resume_upload_new")],
        ])
        await update.message.reply_text(
            f"Welcome back {first_name}! 👋\n\n"
            f"I found your saved resume: *{saved_pdf.stem}*\n\n"
            "What would you like to do with it?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
        return WAITING_RESUME
    else:
        await update.message.reply_text(
            f"Hi {first_name}! I am your *AI Resume Optimizer* 🤖\n\n"
            "I tailor your resume to job postings, help you find relevant roles, "
            "and keep your resume sharp.\n\n"
            "To get started, please send me your *resume PDF or DOCX* 👇",
            parse_mode=ParseMode.MARKDOWN,
        )
        return WAITING_RESUME

async def handle_resume_use_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _show_main_menu(query.message, context)
    return WAITING_MAIN_CHOICE

async def handle_resume_view_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    pdf_path = context.user_data.get("resume_path")
    if pdf_path and Path(pdf_path).exists():
        with open(pdf_path, "rb") as f:
            await query.message.reply_document(document=f, filename=Path(pdf_path).name,
                                               caption="Your saved resume 📎")
    else:
        await query.message.reply_text("Could not find saved resume file.")
    await _show_main_menu(query.message, context)
    return WAITING_MAIN_CHOICE

async def handle_resume_upload_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    context.user_data.pop("resume_path", None)
    context.user_data.pop("resume_text", None)
    await query.message.reply_text("Please send your new resume PDF or DOCX 👇")
    return WAITING_RESUME

async def handle_resume_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send a PDF or DOCX file.")
        return WAITING_RESUME
    fname = doc.file_name or "resume"
    ext   = Path(fname).suffix.lower()
    if ext not in (".pdf", ".docx", ".doc"):
        await update.message.reply_text("Only PDF or DOCX files are supported.")
        return WAITING_RESUME
    await update.message.reply_text("⬇️ Downloading your resume...")
    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    file       = await doc.get_file()
    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_path = TEMP_DIR / f"{update.effective_user.id}_{ts}{ext}"
    await file.download_to_drive(str(local_path))
    context.user_data["resume_path"] = str(local_path)
    context.user_data["resume_name"] = fname
    context.user_data.pop("resume_text", None)
    await update.message.reply_text("✅ Resume uploaded!")
    await _show_main_menu(update.message, context)
    return WAITING_MAIN_CHOICE

async def _show_main_menu(message, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Update My Resume",  callback_data="action_update_resume")],
        [InlineKeyboardButton("🔍 Looking for a Job", callback_data="action_find_job")],
        [InlineKeyboardButton("🔗 I Have a Job Link", callback_data="action_job_link")],
    ])
    await message.reply_text(
        "*What would you like to do?*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )

# ═══════════════════════════════════════════════════════════════════════════════
# PATH A — Update Resume
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_action_update_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update.effective_user.id):
        await query.message.reply_text(
            f"Free limit reached ({FREE_LIMIT} uses). Send your access key to unlock unlimited access."
        )
        return ConversationHandler.END
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    resume_path = resolve_resume_path(context, query.from_user.id)
    resume_text = context.user_data.get("resume_text", "")
    msg = await query.message.reply_text("📄 Analyzing your resume...")
    if not resume_text:
        if not resume_path:
            await msg.edit_text("❌ Resume file not found. Please /start and upload again.")
            return ConversationHandler.END
        resume_text = parse_resume_with_gemini(resume_path)
        if not resume_text or resume_text.lower().startswith("error"):
            await msg.edit_text(f"Could not parse resume: {resume_text}")
            return ConversationHandler.END
        context.user_data["resume_text"] = resume_text
    try:
        analysis = call_llm(build_resume_analysis_prompt(resume_text), FLASH_MODEL)
    except Exception as e:
        await msg.edit_text(f"Analysis failed: {e}")
        return ConversationHandler.END
    quality  = analysis.get("overall_quality", "")
    summary  = analysis.get("summary", "")
    missing  = analysis.get("missing_sections", [])
    weak     = analysis.get("weak_sections", [])
    info_gap = analysis.get("missing_info", [])
    feedback = f"📊 *Resume Analysis*\n\n_{summary}_\n\nQuality: *{quality}*\n"
    if missing:
        feedback += "\n❌ *Missing sections:*\n" + "\n".join(f"  - {s}" for s in missing)
    if weak:
        feedback += "\n\n⚠️ *Weak areas:*\n"
        for w in weak[:3]:
            feedback += f"  - *{w['section']}*: {w['issue']}\n"
    if info_gap:
        feedback += "\n\n💡 *Missing info:*\n" + "\n".join(f"  - {s}" for s in info_gap[:4])
    await msg.edit_text(feedback, parse_mode=ParseMode.MARKDOWN)
    await query.message.reply_text(
        "Tell me what you'd like to add or change.\n\n"
        "You can *type* or send a *voice note* — describe everything in one go:\n"
        "_e.g. 'I joined Zepto as Senior DS in Jan 2025, working on demand forecasting "
        "using XGBoost and PySpark. Also completed AWS ML Specialty cert.'_",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAITING_RESUME_UPDATE

async def handle_resume_update_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    return await _process_resume_update(update.message, context, user_input)

async def handle_resume_update_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles voice in WAITING_RESUME_UPDATE and WAITING_UPDATE_CONFIRM states."""
    msg = await update.message.reply_text("🎙️ Transcribing your voice note...")
    voice = update.message.voice
    file  = await voice.get_file()
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    ogg   = TEMP_DIR / f"{update.effective_user.id}_{ts}.ogg"
    await file.download_to_drive(str(ogg))
    transcript = await transcribe_voice(str(ogg))
    ogg.unlink(missing_ok=True)
    logger.info(f"Resume update voice transcription: {transcript[:100]}")
    if transcript.startswith("[Transcription failed"):
        await msg.edit_text(f"Could not transcribe voice note.\n{transcript}\n\nPlease type your update instead.")
        return WAITING_RESUME_UPDATE
    await msg.edit_text(f"🎙️ Transcribed: _{transcript}_", parse_mode=ParseMode.MARKDOWN)
    return await _process_resume_update(update.message, context, transcript)

async def _process_resume_update(message, context, user_input: str):
    resume_text = context.user_data.get("resume_text", "")
    if not resume_text:
        resume_path = resolve_resume_path(context, message.chat.id)
        msg_parse   = await message.reply_text("📄 Parsing your resume...")
        if not resume_path:
            await msg_parse.edit_text("❌ Resume file not found. Please /start and upload again.")
            return WAITING_RESUME_UPDATE
        resume_text = parse_resume_with_gemini(resume_path)
        if not resume_text or resume_text.lower().startswith("error"):
            await msg_parse.edit_text(f"❌ Could not parse resume: {resume_text}")
            return WAITING_RESUME_UPDATE
        context.user_data["resume_text"] = resume_text
        await msg_parse.edit_text("✅ Resume parsed.")
    msg = await message.reply_text("🔀 Merging your update...")
    try:
        merge_result  = call_llm(build_resume_update_prompt(resume_text, user_input), LITE_MODEL)
        merged_md     = merge_result.get("optimized_resume_text", "")
        merge_changes = merge_result.get("changes_made", [])
        total_exp     = merge_result.get("total_experience", "")
    except Exception as e:
        await msg.edit_text(f"❌ Merge failed: {e}")
        return WAITING_RESUME_UPDATE
    if not merged_md:
        await msg.edit_text("❌ Merge returned nothing. Please try again.")
        return WAITING_RESUME_UPDATE
    context.user_data["merged_resume_md"] = merged_md
    context.user_data["total_experience"] = total_exp
    context.user_data["is_update_mode"]   = True
    changes_text = "\n".join(f"  • {c}" for c in merge_changes[:4])
    exp_line     = f"\n🗓 *Total experience: {total_exp}*" if total_exp else ""
    await msg.edit_text(
        f"✅ *Updates merged!*{exp_line}\n\n*What was added/changed:*\n{changes_text}",
        parse_mode=ParseMode.MARKDOWN,
    )
    await message.reply_text("*Preview of merged resume:*", parse_mode=ParseMode.MARKDOWN)
    chunk_size = 3800
    for i in range(0, len(merged_md), chunk_size):
        part = merged_md[i:i+chunk_size]
        await message.reply_text(f"```\n{part}\n```", parse_mode=ParseMode.MARKDOWN)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Looks good — Generate Resume", callback_data="update_confirm_go")],
        [InlineKeyboardButton("✏️ Add more info first",          callback_data="update_confirm_more")],
    ])
    await message.reply_text(
        "Does this look right?\n\n"
        "_Tap *Add more info* if anything is missing or wrong._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )
    return WAITING_UPDATE_CONFIRM

# ── Update confirm handlers ───────────────────────────────────────────────────

async def handle_update_confirm_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    merged_md = context.user_data.get("merged_resume_md", "")
    if not merged_md:
        await query.message.reply_text("❌ Merged content lost. Please /start again.")
        return ConversationHandler.END
    context.user_data["resume_text"] = merged_md
    return await _run_update_rewrite(query.message, context)

async def handle_update_confirm_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(
        "What else would you like to add or correct?\n\n"
        "_Type or send a voice note — I'll merge it with what we have so far._",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAITING_UPDATE_CONFIRM

async def handle_update_additional_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    additional = update.message.text.strip()
    current_md = context.user_data.get("merged_resume_md", "")
    if not current_md:
        await update.message.reply_text("❌ No resume content found. Please /start again.")
        return ConversationHandler.END
    msg = await update.message.reply_text("🔀 Merging additional info...")
    try:
        merge_result = call_llm(build_resume_update_prompt(current_md, additional), LITE_MODEL)
        updated_md   = merge_result.get("optimized_resume_text", "")
        new_changes  = merge_result.get("changes_made", [])
        total_exp    = merge_result.get("total_experience", context.user_data.get("total_experience",""))
    except Exception as e:
        await msg.edit_text(f"❌ Merge failed: {e}")
        return WAITING_UPDATE_CONFIRM
    if not updated_md:
        await msg.edit_text("❌ Merge returned nothing. Please try again.")
        return WAITING_UPDATE_CONFIRM
    context.user_data["merged_resume_md"] = updated_md
    if total_exp:
        context.user_data["total_experience"] = total_exp
    changes_text = "\n".join(f"  • {c}" for c in new_changes[:3])
    exp_line     = f"\n🗓 *Total experience: {total_exp}*" if total_exp else ""
    await msg.edit_text(
        f"✅ *Additional info merged!*{exp_line}\n\n*Changes:*\n{changes_text}",
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.message.reply_text("*Updated preview:*", parse_mode=ParseMode.MARKDOWN)
    chunk_size = 3800
    for i in range(0, len(updated_md), chunk_size):
        part = updated_md[i:i+chunk_size]
        await update.message.reply_text(f"```\n{part}\n```", parse_mode=ParseMode.MARKDOWN)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Looks good — Generate Resume", callback_data="update_confirm_go")],
        [InlineKeyboardButton("✏️ Add more info first",          callback_data="update_confirm_more")],
    ])
    await update.message.reply_text("Ready to generate?", reply_markup=keyboard)
    return WAITING_UPDATE_CONFIRM

async def handle_update_confirm_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Voice note sent while in WAITING_UPDATE_CONFIRM — merge as additional info."""
    msg = await update.message.reply_text("🎙️ Transcribing your voice note...")
    voice = update.message.voice
    file  = await voice.get_file()
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    ogg   = TEMP_DIR / f"{update.effective_user.id}_{ts}.ogg"
    await file.download_to_drive(str(ogg))
    transcript = await transcribe_voice(str(ogg))
    ogg.unlink(missing_ok=True)
    logger.info(f"Update confirm voice transcription: {transcript[:100]}")
    if transcript.startswith("[Transcription failed"):
        await msg.edit_text("Could not transcribe. Please type your update instead.")
        return WAITING_UPDATE_CONFIRM
    await msg.edit_text(f"🎙️ Transcribed: _{transcript}_", parse_mode=ParseMode.MARKDOWN)
    current_md = context.user_data.get("merged_resume_md", "")
    if not current_md:
        await update.message.reply_text("❌ No resume content found. Please /start again.")
        return ConversationHandler.END
    msg2 = await update.message.reply_text("🔀 Merging...")
    try:
        merge_result = call_llm(build_resume_update_prompt(current_md, transcript), FLASH_MODEL)
        updated_md   = merge_result.get("optimized_resume_text", "")
        new_changes  = merge_result.get("changes_made", [])
        total_exp    = merge_result.get("total_experience", context.user_data.get("total_experience",""))
    except Exception as e:
        await msg2.edit_text(f"❌ Merge failed: {e}")
        return WAITING_UPDATE_CONFIRM
    if not updated_md:
        await msg2.edit_text("❌ Merge returned nothing. Please try again.")
        return WAITING_UPDATE_CONFIRM
    context.user_data["merged_resume_md"] = updated_md
    if total_exp:
        context.user_data["total_experience"] = total_exp
    changes_text = "\n".join(f"  • {c}" for c in new_changes[:3])
    await msg2.edit_text(
        f"✅ *Merged!*\n\n*Changes:*\n{changes_text}",
        parse_mode=ParseMode.MARKDOWN,
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Looks good — Generate Resume", callback_data="update_confirm_go")],
        [InlineKeyboardButton("✏️ Add more info first",          callback_data="update_confirm_more")],
    ])
    await update.message.reply_text("Ready to generate?", reply_markup=keyboard)
    return WAITING_UPDATE_CONFIRM

# ═══════════════════════════════════════════════════════════════════════════════
# PATH B — Looking for a Job
# ═══════════════════════════════════════════════════════════════════════════════

JOB_ROLES    = ["Data Scientist", "ML Engineer", "AI Engineer", "Data Analyst",
                "Data Engineer", "MLOps Engineer"]
JOB_CITIES   = ["Bengaluru", "Hyderabad", "Mumbai", "Delhi", "Chennai", "Pune", "Remote"]
EXP_LEVELS   = ["0-2 years", "2-5 years", "5-8 years", "8+ years"]
TIME_FILTERS = {
    "Last 3 hours": "r10800",
    "Last 1 day":   "r86400",
    "Last 7 days":  "r604800",
}

async def handle_action_find_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update.effective_user.id):
        await query.message.reply_text(
            f"Free limit reached ({FREE_LIMIT} uses). Send your access key to unlock unlimited access."
        )
        return ConversationHandler.END
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(r, callback_data=f"jp_role_{r}")] for r in JOB_ROLES
    ])
    await query.message.reply_text("*What role are you looking for?*",
                                   parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    return WAITING_JOB_PREFS

async def handle_jp_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    role  = query.data.replace("jp_role_", "")
    context.user_data["jp_role"] = role
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(c, callback_data=f"jp_city_{c}")] for c in JOB_CITIES
    ])
    await query.edit_message_text(f"*{role}* — Which city?",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    return WAITING_JOB_PREFS

async def handle_jp_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    city  = query.data.replace("jp_city_", "")
    context.user_data["jp_city"] = city
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(e, callback_data=f"jp_exp_{e}")] for e in EXP_LEVELS
    ])
    await query.edit_message_text(
        f"*{context.user_data['jp_role']}* in *{city}* — Your experience level?",
        parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    return WAITING_JOB_PREFS

async def handle_jp_exp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    exp   = query.data.replace("jp_exp_", "")
    context.user_data["jp_exp"] = exp
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"jp_time_{label}")] for label in TIME_FILTERS
    ])
    await query.edit_message_text(
        f"*{context.user_data['jp_role']}* · *{context.user_data['jp_city']}* · *{exp}*\n\n"
        "How recent should the postings be?",
        parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    return WAITING_JOB_TIME

async def handle_jp_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query       = update.callback_query
    await query.answer()
    time_label  = query.data.replace("jp_time_", "")
    time_filter = TIME_FILTERS.get(time_label, "r86400")
    role        = context.user_data.get("jp_role", "data scientist")
    city        = context.user_data.get("jp_city", "Bengaluru")
    exp         = context.user_data.get("jp_exp", "")
    resume_text = context.user_data.get("resume_text", "")
    await query.edit_message_text(
        f"Searching *{role}* in *{city}* ({time_label.lower()})...",
        parse_mode=ParseMode.MARKDOWN)
    try:
        kw   = f"{role} {exp}" if city == "Bengaluru" else f"{role} {city}"
        jobs = fetch_jobs(keywords=kw, time_filter=time_filter, max_jobs=15, big_only=True)
        if not jobs:
            jobs = fetch_jobs(keywords=kw, time_filter=time_filter, max_jobs=15, big_only=False)
    except Exception as e:
        await query.message.reply_text(f"Could not fetch jobs: {e}")
        return ConversationHandler.END
    if not jobs:
        await query.message.reply_text(
            "No jobs found for that filter.\n\nTry a wider time range.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Try last 7 days", callback_data="jp_time_Last 7 days")
            ]]))
        return WAITING_JOB_TIME
    await query.message.reply_text(f"Found {len(jobs)} jobs. Analyzing against your resume... 🤖")
    job_dicts = [{"title": j.title, "company": j.company, "url": j.url, "job_id": j.job_id or str(i)}
                 for i, j in enumerate(jobs)]
    ranked = job_dicts
    if resume_text:
        try:
            rank_result = call_llm(build_job_ranking_prompt(job_dicts, resume_text), LITE_MODEL)
            ranked = rank_result.get("ranked_jobs", job_dicts)
        except Exception as e:
            logger.warning(f"Job ranking failed: {e}")
    await query.message.reply_text(
        f"*{role} — {city} — {time_label}*\n_{len(ranked)} jobs ranked by match_",
        parse_mode=ParseMode.MARKDOWN)
    job_map = {}
    for i, job in enumerate(ranked[:10], 1):
        jid       = job.get("job_id") or str(i)
        url       = job.get("url", "")
        score     = job.get("match_score", "")
        reason    = job.get("reason", "")
        company   = job.get("company", "")
        title     = job.get("title", "")
        score_line = f"🎯 *{score}% match* — {reason}" if score else ""
        clean_url  = url.split("?")[0] if url else ""
        job_map[jid] = {"url": clean_url, "title": title, "company": company}
        await query.message.reply_text(
            f"*{i}. {title}*\n{company}\n{score_line}\n{clean_url}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Optimize my resume for this", callback_data=f"optimize_{jid}")
            ]]),
            disable_web_page_preview=True,
        )
    context.user_data["job_map"] = job_map
    await query.message.reply_text("Tap *Optimize my resume for this* on any job above.",
                                   parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════════════════════════════
# PATH C — Job Link
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_action_job_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update.effective_user.id):
        await query.message.reply_text(
            f"Free limit reached ({FREE_LIMIT} uses). Send your access key to unlock unlimited access."
        )
        return ConversationHandler.END
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(
        "Please paste the job posting URL 👇\n\n"
        "_If the URL doesn't work, you can also paste the job description as text._",
        parse_mode=ParseMode.MARKDOWN)
    return WAITING_JOB_URL

# ═══════════════════════════════════════════════════════════════════════════════
# OPTIMIZATION PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_job_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("That doesn't look like a URL. Send the full https:// link.")
        return WAITING_JOB_URL
    context.user_data["job_url"] = url
    msg = await update.message.reply_text("🌐 Scraping job description...")
    await update.message.chat.send_action(ChatAction.TYPING)
    jd = scrape_url_content(url)
    if not jd:
        await msg.edit_text("❌ Couldn't scrape that URL.\n\n💡 Paste the job description text directly instead.")
        context.user_data["waiting_for_manual_jd"] = True
        return WAITING_JOB_URL
    context.user_data["job_description"] = jd
    return await _run_analysis(update.message, msg, context)

async def handle_text_in_url_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.startswith("http"):
        return await handle_job_url(update, context)
    elif context.user_data.get("waiting_for_manual_jd") or len(text) > 200:
        return await handle_manual_jd(update, context)
    else:
        await update.message.reply_text("Send the job URL (https://...) or paste the full job description.")
        return WAITING_JOB_URL

async def handle_manual_jd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_manual_jd"):
        return await handle_job_url(update, context)
    text = update.message.text.strip()
    if len(text) < 100:
        await update.message.reply_text("Too short — paste the full job description.")
        return WAITING_JOB_URL
    context.user_data["job_description"] = text
    context.user_data.pop("waiting_for_manual_jd", None)
    msg = await update.message.reply_text("✅ Got the job description!")
    return await _run_analysis(update.message, msg, context)

async def _run_analysis(message, status_msg, context):
    await status_msg.edit_text("📄 Parsing your resume...")
    resume_path = resolve_resume_path(context, message.chat.id)
    resume_text = context.user_data.get("resume_text", "")
    if not resume_text:
        if not resume_path:
            await status_msg.edit_text("❌ Resume file not found. Please /start and upload again.")
            return ConversationHandler.END
        resume_text = parse_resume_with_gemini(resume_path)
        if not resume_text or resume_text.lower().startswith("error"):
            await status_msg.edit_text(f"❌ Failed to parse resume: {resume_text}")
            return ConversationHandler.END
        context.user_data["resume_text"] = resume_text
    jd = context.user_data.get("job_description", "")
    await status_msg.edit_text("🔍 Analyzing match...")
    try:
        analysis = call_llm(build_analysis_prompt(jd, resume_text), FLASH_MODEL)
    except Exception as e:
        await status_msg.edit_text(f"❌ Analysis failed: {e}")
        return ConversationHandler.END
    score       = analysis.get("score", 0)
    match_level = analysis.get("match_level", "Unknown")
    context.user_data["analysis"] = analysis
    context.user_data["score"]    = score
    await status_msg.edit_text(fmt_analysis(analysis, score, match_level), parse_mode=ParseMode.MARKDOWN)
    if score < REWRITE_THRESHOLD:
        await message.reply_text("⏳ Generating improvement roadmap...")
        try:
            guidance = call_llm(build_low_score_guidance_prompt(jd, resume_text, analysis), LITE_MODEL)
        except Exception as e:
            await message.reply_text(f"❌ Guidance failed: {e}")
            return ConversationHandler.END
        await message.reply_text(fmt_roadmap(guidance), parse_mode=ParseMode.MARKDOWN)
        await message.reply_text(
            "*What next?*\n\n🔄 /new — Try a different job\n🔍 /start — Back to main menu",
            parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    await message.reply_text("⏳ Preparing questions...")
    try:
        q_result = call_llm(build_clarifying_questions_prompt(jd, resume_text, analysis), LITE_MODEL)
    except Exception:
        q_result = {"should_ask": False, "questions": []}
    questions  = q_result.get("questions", [])
    should_ask = q_result.get("should_ask", False) and len(questions) > 0
    if should_ask:
        context.user_data["questions"]      = questions
        context.user_data["current_q_idx"]  = 0
        context.user_data["clarifications"] = {}
        first_q = questions[0]["question"]
        context.user_data["last_question"]  = first_q
        skip_btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("Skip this question", callback_data="skip_question")
        ]])
        await message.reply_text(
            f"Quick question (1/{len(questions)}):\n\n*{first_q}*\n\n_Answer honestly._",
            parse_mode=ParseMode.MARKDOWN, reply_markup=skip_btn)
        return WAITING_CLARIFICATION
    return await _ask_extra_skills_msg(message, context)

async def _process_clarification(message, context, answer: str):
    questions      = context.user_data.get("questions", [])
    current_idx    = context.user_data.get("current_q_idx", 0)
    clarifications = context.user_data.get("clarifications", {})
    last_q         = context.user_data.get("last_question", "")
    if answer.lower() != "skip" and answer:
        clarifications[last_q] = answer
        context.user_data["clarifications"] = clarifications
    next_idx = current_idx + 1
    context.user_data["current_q_idx"] = next_idx
    if next_idx < len(questions):
        next_q = questions[next_idx]["question"]
        context.user_data["last_question"] = next_q
        skip_btn = InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="skip_question")]])
        await message.reply_text(
            f"Question ({next_idx+1}/{len(questions)}):\n\n*{next_q}*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=skip_btn)
        return WAITING_CLARIFICATION
    return await _ask_extra_skills_msg(message, context)

async def handle_clarification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text.strip() if update.message.text else ""
    return await _process_clarification(update.message, context, answer)

async def handle_clarification_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Voice note answer to a clarifying question."""
    msg   = await update.message.reply_text("🎙️ Transcribing...")
    voice = update.message.voice
    file  = await voice.get_file()
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    ogg   = TEMP_DIR / f"{update.effective_user.id}_{ts}.ogg"
    await file.download_to_drive(str(ogg))
    transcript = await transcribe_voice(str(ogg))
    ogg.unlink(missing_ok=True)
    logger.info(f"Clarification voice transcription: {transcript[:100]}")
    if transcript.startswith("[Transcription failed"):
        await msg.edit_text("Could not transcribe. Please type your answer instead.")
        return WAITING_CLARIFICATION
    await msg.edit_text(f"🎙️ Transcribed: _{transcript}_", parse_mode=ParseMode.MARKDOWN)
    return await _process_clarification(update.message, context, transcript)  # ← fixed: return added

async def handle_skip_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query       = update.callback_query
    await query.answer()
    questions   = context.user_data.get("questions", [])
    current_idx = context.user_data.get("current_q_idx", 0)
    next_idx    = current_idx + 1
    context.user_data["current_q_idx"] = next_idx
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if next_idx < len(questions):
        next_q = questions[next_idx]["question"]
        context.user_data["last_question"] = next_q
        skip_btn = InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="skip_question")]])
        await query.message.reply_text(
            f"Question ({next_idx+1}/{len(questions)}):\n\n*{next_q}*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=skip_btn)
        return WAITING_CLARIFICATION
    return await _ask_extra_skills_msg(query.message, context)

async def _ask_extra_skills_msg(message, context):
    analysis    = context.user_data.get("analysis") or {}
    all_missing = (analysis.get("missing_critical",[]) + analysis.get("missing_preferred",[]))[:6]
    if not all_missing:
        return await _run_rewrite(message, context)
    skills_text = "\n".join(f"  • {s}" for s in all_missing)
    context.user_data["missing_skills_asked"] = all_missing
    none_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("I don't have any of these", callback_data="no_extra_skills")
    ]])
    await message.reply_text(
        f"*One last check* — in JD but not your resume:\n\n{skills_text}\n\n"
        "Have any of these? Type comma-separated, or tap below.\n"
        "_Example: Spark, A/B testing, dbt_",
        parse_mode=ParseMode.MARKDOWN, reply_markup=none_btn)
    return WAITING_SKILLS_INPUT

async def handle_skills_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text.strip()
    extra  = []
    if answer.lower() not in ("none","no","skip","-"):
        extra = [s.strip() for s in answer.replace(";",",").split(",") if s.strip()]
    context.user_data["extra_skills"] = extra
    if extra:
        await update.message.reply_text(f"✅ Adding: *{', '.join(extra)}*", parse_mode=ParseMode.MARKDOWN)
    return await _run_rewrite(update.message, context)

async def handle_no_extra_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    context.user_data["extra_skills"] = []
    return await _run_rewrite(query.message, context)

async def _run_update_rewrite(message, context):
    user_id = message.chat.id
    if not await _check_limit_and_gate(message, user_id):
        return WAITING_ATS_CONFIRM
    merged_text = context.user_data.get("resume_text", "")
    if not merged_text:
        await message.reply_text("❌ No resume text found. Please try /start again.")
        return ConversationHandler.END
    msg = await message.reply_text("✍️ Step 1/3: Rewriting content... ~30 seconds.")
    await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    try:
        total_exp = context.user_data.get("total_experience", "")
        loop      = asyncio.get_event_loop()
        rewrite   = await loop.run_in_executor(
            None, lambda: call_llm(build_update_rewrite_prompt(merged_text, total_exp), PRO_MODEL)
        )
    except Exception as e:
        await msg.edit_text(f"❌ Rewrite failed: {e}")
        return ConversationHandler.END
    resume_md = rewrite.get("optimized_resume_text", "")
    changes   = rewrite.get("changes_made", [])
    total_exp = context.user_data.get("total_experience", "")
    if not resume_md:
        await msg.edit_text("❌ No resume returned. Please try /start again.")
        return ConversationHandler.END

    # Step 2+3: Format + Verify chain
    await msg.edit_text("✍️ Step 2/3: Fixing format + verifying names...")
    resume_md, chain_changes = await chain_rewrite(resume_md)
    changes += chain_changes
    import re as _re
    resume_md = _re.sub(r'\s*\(metric needed\)', '', resume_md)
    resume_md = _re.sub(r'\s*\(needs metric\)', '', resume_md)
    resume_md = _re.sub(r'\s*\[metric needed\]', '', resume_md)
    cleanup_output_dir()
    await msg.edit_text("📄 Generating PDF...")
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_path = str(OUTPUT_DIR / f"{message.chat.id}_{ts}_updated.pdf")
    try:
        generate_resume_pdf(resume_md, pdf_path)
    except Exception as e:
        await msg.edit_text(f"⚠️ PDF failed: {e}")
        return ConversationHandler.END
    changes_text = "\n".join(f"  • {c}" for c in changes[:4])
    exp_line     = f"\n🗓 *Total experience: {total_exp}*" if total_exp else ""
    await msg.edit_text(
        f"✅ *Resume Updated & Ready!*{exp_line}\n\n📝 *Writing improvements:*\n{changes_text}",
        parse_mode=ParseMode.MARKDOWN)
    with open(pdf_path, "rb") as f:
        await message.reply_document(document=f, filename=f"updated_resume_{ts}.pdf",
                                     caption="📎 Your updated resume")
    try:
        saved = save_resume_for_user(message.chat.id, pdf_path, resume_md)
        context.user_data["resume_path"]    = str(saved)
        context.user_data["resume_text"]    = resume_md
        context.user_data["last_pdf_path"]  = str(saved)
        context.user_data["last_resume_md"] = resume_md
        logger.info(f"Saved updated: {saved}")
    except Exception as e:
        logger.warning(f"Profile save failed: {e}")
    access    = increment_use(message.chat.id)
    remaining = FREE_LIMIT - access.get("uses", 0)
    if not access.get("unlocked") and remaining > 0:
        await message.reply_text(f"_You have {remaining} free generation(s) remaining._",
                                 parse_mode=ParseMode.MARKDOWN)
    ats_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, run ATS check", callback_data="ats_yes")],
        [InlineKeyboardButton("⏭️ Skip for now",       callback_data="ats_skip")],
    ])
    await message.reply_text(
        "Would you like to run an ATS check on the updated resume?\n\n"
        "_Checks parse rate, keywords, formatting. Auto-fixes issues if found._\n\n"
        "⏱ Takes about 2-3 minutes.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=ats_btn)
    return WAITING_ATS_CONFIRM

async def _run_rewrite(message, context):
    user_id = message.chat.id
    if not await _check_limit_and_gate(message, user_id):
        return WAITING_ATS_CONFIRM
    jd             = context.user_data.get("job_description","")
    resume_text    = context.user_data.get("resume_text","")
    analysis       = context.user_data.get("analysis",{})
    clarifications = context.user_data.get("clarifications",{})
    extra_skills   = context.user_data.get("extra_skills",[])
    score          = context.user_data.get("score",0)
    msg = await message.reply_text("✍️ Step 1/3: Tailoring content... ~30 seconds.")
    await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    try:
        loop    = asyncio.get_event_loop()
        rewrite = await loop.run_in_executor(
            None, lambda: call_llm(
                build_rewrite_with_context_prompt(jd, resume_text, analysis, clarifications, extra_skills),
                PRO_MODEL)
        )
    except Exception as e:
        await msg.edit_text(f"❌ Rewrite failed: {e}")
        return ConversationHandler.END
    resume_md        = rewrite.get("optimized_resume_text","")
    changes          = rewrite.get("changes_made",[])
    est_score        = rewrite.get("final_score_estimate","?")
    cover_hook       = rewrite.get("cover_letter_hook","")
    missing_keywords = rewrite.get("missing_keywords",[])
    if not resume_md:
        await msg.edit_text("❌ No resume returned. Try /start")
        return ConversationHandler.END

    # Step 2+3: Format + Verify chain
    await msg.edit_text("✍️ Step 2/3: Fixing format + verifying names...")
    resume_md, chain_changes = await chain_rewrite(resume_md)
    changes += chain_changes
    import re as _re
    resume_md = _re.sub(r'\s*\(metric needed\)', '', resume_md)
    resume_md = _re.sub(r'\s*\(needs metric\)', '', resume_md)
    resume_md = _re.sub(r'\s*\[metric needed\]', '', resume_md)
    cleanup_output_dir()
    await msg.edit_text("📄 Generating PDF...")
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_path = str(OUTPUT_DIR / f"{message.chat.id}_{ts}_tailored.pdf")
    try:
        generate_resume_pdf(resume_md, pdf_path)
    except Exception as e:
        await msg.edit_text(f"⚠️ PDF failed: {e}")
        await message.reply_text(f"```\n{resume_md[:3000]}\n```", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    changes_text = "\n".join(f"  • {c}" for c in changes[:4])
    is_update    = context.user_data.get("is_update_mode", False)
    if is_update:
        summary = f"✅ *Resume Updated & Formatted!*\n\n📝 *Changes applied:*\n{changes_text}\n"
    else:
        summary = (f"✅ *Resume Tailored!*\n\nScore: *{score}/100* → *{est_score}/100* (estimated)\n\n"
                   f"📝 *Key changes:*\n{changes_text}\n")
        if cover_hook:
            summary += f"\n✉️ *Cover letter opener:*\n_{cover_hook}_"
        if missing_keywords:
            kw_text = ", ".join(missing_keywords[:8])
            summary += f"\n\n⚠️ *Missing JD keywords:* `{kw_text}`"
    await msg.edit_text(summary, parse_mode=ParseMode.MARKDOWN)
    with open(pdf_path,"rb") as f:
        await message.reply_document(document=f, filename=f"tailored_resume_{ts}.pdf",
                                     caption="📎 Your ATS-optimized tailored resume")
    try:
        saved = save_resume_for_user(message.chat.id, pdf_path, resume_md)
        context.user_data["last_pdf_path"]  = str(saved)
        context.user_data["last_resume_md"] = resume_md
        logger.info(f"Saved tailored: {saved}")
    except Exception as e:
        logger.warning(f"Profile save failed: {e}")
    access    = increment_use(message.chat.id)
    remaining = FREE_LIMIT - access.get("uses", 0)
    if not access.get("unlocked") and remaining > 0:
        await message.reply_text(f"_You have {remaining} free generation(s) remaining._",
                                 parse_mode=ParseMode.MARKDOWN)
    ats_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, run ATS check", callback_data="ats_yes")],
        [InlineKeyboardButton("⏭️ Skip for now",       callback_data="ats_skip")],
    ])
    await message.reply_text(
        "Would you like to run an ATS check?\n\n"
        "_This analyzes parse rate, keyword coverage, and formatting. "
        "If issues are found, I'll auto-fix and send a v2._\n\n"
        "⏱ Takes about 2-3 minutes.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=ats_btn)
    return WAITING_ATS_CONFIRM

async def handle_ats_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update.effective_user.id):
        await query.message.reply_text(
            f"Free limit reached ({FREE_LIMIT} uses). Send your access key to unlock unlimited access."
        )
        return ConversationHandler.END
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    pdf_path  = context.user_data.get("last_pdf_path","")
    resume_md = context.user_data.get("last_resume_md","")
    if not pdf_path or not Path(pdf_path).exists():
        await query.message.reply_text("Could not find the PDF. Please try /start again.")
        return ConversationHandler.END
    await _run_ats_and_fix(query.message, context, resume_md, pdf_path)
    return await show_apply_prompt(query.message, context)

async def handle_ats_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text("Skipped ATS check.")
    return await show_apply_prompt(query.message, context)

async def _show_final_next_steps(message):
    await message.reply_text(
        "What next?\n\n"
        "/start — Back to main menu\n"
        "/my_resume — Download saved resumes\n"
        "/help — See all commands",
    )

async def handle_optimize_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    jid   = query.data.replace("optimize_","")
    job   = context.user_data.get("job_map",{}).get(jid)
    if not job:
        await query.message.reply_text("Could not find that job. Use /start to search again.")
        return ConversationHandler.END
    title   = job.get("title","this role")
    company = job.get("company","")
    url     = job.get("url","")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    context.user_data["job_url"]      = url
    context.user_data["selected_job"] = job
    context.user_data.pop("job_description", None)
    await query.message.reply_text(
        f"Optimizing for:\n*{title}* at *{company}*", parse_mode=ParseMode.MARKDOWN)
    chat_msg = await query.message.reply_text("🌐 Scraping job description...")
    jd = scrape_url_content(url)
    if not jd:
        await chat_msg.edit_text("Couldn't scrape.\nPaste the job description as text here.")
        context.user_data["waiting_for_manual_jd"] = True
        return WAITING_JOB_URL
    context.user_data["job_description"] = jd
    return await _run_analysis(query.message, chat_msg, context)

# ── /my_resume ────────────────────────────────────────────────────────────────

async def cmd_my_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = getattr(update, 'message', None) or update.callback_query.message
    user_id = update.effective_user.id
    resumes = list_user_resumes(user_id)
    if not resumes:
        await message.reply_text("No saved resumes yet.\n\nUse /start to optimize a resume — it's saved automatically.")
        return
    if len(resumes) == 1:
        with open(resumes[0],"rb") as f:
            await message.reply_document(document=f, filename=resumes[0].name,
                                         caption="Your latest resume 📎")
        return
    keyboard = []
    for i, pdf in enumerate(resumes[:5], 1):
        parts = pdf.stem.split("_")
        ver   = parts[1] if len(parts) > 1 else f"v{i}"
        date  = parts[2][:8] if len(parts) > 2 else ""
        keyboard.append([InlineKeyboardButton(f"📄 {ver} — {date}", callback_data=f"dl_resume_{pdf.name}")])
    keyboard.append([InlineKeyboardButton("📥 Download Latest", callback_data=f"dl_resume_{resumes[0].name}")])
    await message.reply_text(f"*Your Saved Resumes* ({len(resumes)} versions)",
                             parse_mode=ParseMode.MARKDOWN,
                             reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_download_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    filename = query.data.replace("dl_resume_","")
    pdf_path = get_user_dir(update.effective_user.id) / filename
    if not pdf_path.exists():
        await query.message.reply_text("That version is no longer available.")
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    with open(pdf_path,"rb") as f:
        await query.message.reply_document(document=f, filename=filename, caption=f"📎 {filename}")

# ── Misc commands ─────────────────────────────────────────────────────────────

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Starting fresh! Send /start to begin.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*AI Resume Optimizer — Help*\n\n"
        "*Flow:*\n"
        "1 /start → upload or use saved resume\n"
        "2 Choose: Update Resume / Find Job / Job Link\n"
        "3 Get a tailored PDF\n"
        "4 Optional ATS check (2-3 mins)\n\n"
        "*Commands:*\n"
        "🚀 /start — Main menu\n"
        "📥 /my_resume — Download saved resumes\n"
        "🔄 /new — Clear session\n"
        "❌ /cancel — Cancel\n"
        "❓ /help — This message\n\n"
        "*Models:*\n"
        "• Gemini Pro → resume rewrite only\n"
        "• Gemini Flash-lite → everything else\n\n"
        "*Voice notes:*\n"
        "• Supported in all input steps — just send a voice note!",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Session cancelled. Send /start to begin again.",
                                    reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def handle_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if update.message.voice:
        # Voice reached fallback — conversation state was lost (bot restart) or
        # message arrived during state transition. Transcribe and route intelligently.
        questions   = context.user_data.get("questions", [])
        current_idx = context.user_data.get("current_q_idx", 0)
        last_q      = context.user_data.get("last_question", "")

        if questions and last_q:
            # We're mid-clarification — route to clarification voice handler
            return await handle_clarification_voice(update, context)

        merged_md = context.user_data.get("merged_resume_md", "")
        if merged_md:
            # We're mid-update — route to update voice handler
            return await handle_update_confirm_voice(update, context)

        resume_text = context.user_data.get("resume_text", "")
        if resume_text:
            # We're mid-update input — route to resume update voice handler
            return await handle_resume_update_voice(update, context)

        # Truly outside any flow
        await update.message.reply_text(
            "I received your voice note, but I'm not sure what you'd like to do.\n\n"
            "Use /start to begin, then send your voice note when prompted."
        )
        return
    text = update.message.text.strip() if update.message.text else ""
    if text and not text.startswith("/"):
        user_id = update.effective_user.id
        if not is_allowed(user_id):
            await handle_access_key(update, context)
            return
    has_resume = bool(context.user_data.get("resume_path") or context.user_data.get("resume_text"))
    if has_resume:
        await update.message.reply_text("Not sure what to do with that.\n\nUse /start to see the main menu.")
    else:
        await update.message.reply_text("Send /start to begin.\n\nI need your resume first.")

async def handle_wrong_in_resume_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip() if update.message.text else ""
    if text.startswith("http"):
        await update.message.reply_text(
            "That looks like a job URL — but I need your *resume file first*.\n\nAttach a PDF or DOCX.",
            parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("Please attach your resume as a PDF or DOCX file.")
    return WAITING_RESUME

async def handle_expired_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "This session has expired. Please type /start to begin a new one.",
        show_alert=True)

async def post_init(app: Application):
    # Set AFTER pickle is restored — prevents overwrite
    app.bot_data["gemini_client"] = gemini_client
    app.bot_data["model"]         = "gemini-3.5-flash"
    app.bot_data["pro_model"]     = "gemini-3.5-flash"   # autonomous decisions in Easy Apply

    await app.bot.set_my_commands([
        ("start",      "Main menu — optimize, find jobs, update resume"),
        ("my_resume",  "Download your saved resumes"),
        ("new",        "Clear current session"),
        ("cancel",     "Cancel"),
        ("help",       "Help and commands"),
    ])
    logger.info("Bot commands registered.")

def main():
    # ── Smart state migration: reset only users stuck in invalid states ────────
    # This runs on startup and fixes corrupted/stale per-user conversation states
    # WITHOUT wiping the entire pickle (which would log everyone out)
    pickle_file = Path("resume_bot_state.pickle")
    VALID_STATES = set(range(12))  # 0-9 match our conversation state constants

    if pickle_file.exists():
        try:
            import pickle
            with open(pickle_file, "rb") as f:
                data = pickle.load(f)

            changed = False
            # PicklePersistence stores conversations under key "conversations"
            # Format: {conv_name: {(chat_id, user_id): state_int}}
            conversations = data.get("conversations", {})
            for conv_name, conv_data in conversations.items():
                for key, state in list(conv_data.items()):
                    if state not in VALID_STATES and state is not None:
                        logger.warning(f"Resetting invalid state {state} for {key}")
                        conv_data[key] = None  # None = outside conversation
                        changed = True

            if changed:
                with open(pickle_file, "wb") as f:
                    pickle.dump(data, f)
                logger.info("Fixed stale conversation states in pickle.")
        except Exception as e:
            logger.warning(f"Could not migrate pickle, deleting it: {e}")
            pickle_file.unlink(missing_ok=True)

    persistence = PicklePersistence(filepath="resume_bot_state.pickle")
    app = Application.builder().token(BOT_TOKEN).persistence(persistence).post_init(post_init).build()

    # Get apply states + handlers — needed for both ConvHandler and global registration
    apply_conv_states, apply_global_handlers, apply_commands = get_apply_handlers()

    conv = ConversationHandler(
        name="resume_conv",
        persistent=True,
        per_message=False,
        entry_points=[
            CommandHandler("start",  cmd_start),
            MessageHandler(filters.Document.ALL, handle_resume_upload),
            CallbackQueryHandler(handle_optimize_job, pattern="^optimize_"),
        ],
        states={
            WAITING_RESUME: [
                CallbackQueryHandler(handle_resume_use_saved,  pattern="^resume_use_saved$"),
                CallbackQueryHandler(handle_resume_view_saved, pattern="^resume_view_saved$"),
                CallbackQueryHandler(handle_resume_upload_new, pattern="^resume_upload_new$"),
                CommandHandler("help",      cmd_help),
                CommandHandler("cancel",    cmd_cancel),
                CommandHandler("my_resume", cmd_my_resume),
                MessageHandler(filters.Document.ALL, handle_resume_upload),
                MessageHandler(filters.PHOTO, handle_fallback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_wrong_in_resume_state),
            ],
            WAITING_MAIN_CHOICE: [
                CallbackQueryHandler(handle_action_update_resume, pattern="^action_update_resume$"),
                CallbackQueryHandler(handle_action_find_job,      pattern="^action_find_job$"),
                CallbackQueryHandler(handle_action_job_link,      pattern="^action_job_link$"),
                CommandHandler("help",      cmd_help),
                CommandHandler("cancel",    cmd_cancel),
                CommandHandler("my_resume", cmd_my_resume),
            ],
            WAITING_RESUME_UPDATE: [
                CommandHandler("cancel", cmd_cancel),
                CommandHandler("help",   cmd_help),
                MessageHandler(filters.VOICE, handle_resume_update_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_resume_update_text),
            ],
            WAITING_JOB_PREFS: [
                CallbackQueryHandler(handle_jp_role, pattern="^jp_role_"),
                CallbackQueryHandler(handle_jp_city, pattern="^jp_city_"),
                CallbackQueryHandler(handle_jp_exp,  pattern="^jp_exp_"),
                CommandHandler("cancel", cmd_cancel),
                CommandHandler("help",   cmd_help),
            ],
            WAITING_JOB_TIME: [
                CallbackQueryHandler(handle_jp_time, pattern="^jp_time_"),
                CommandHandler("cancel", cmd_cancel),
                CommandHandler("help",   cmd_help),
            ],
            WAITING_JOB_URL: [
                CommandHandler("cancel", cmd_cancel),
                CommandHandler("help",   cmd_help),
                MessageHandler(filters.Document.ALL, handle_resume_upload),
                MessageHandler(filters.VOICE, handle_clarification_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_in_url_state),
            ],
            WAITING_CLARIFICATION: [
                CommandHandler("cancel", cmd_cancel),
                CommandHandler("help",   cmd_help),
                CallbackQueryHandler(handle_skip_question, pattern="^skip_question$"),
                MessageHandler(filters.VOICE, handle_clarification_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_clarification),
            ],
            WAITING_SKILLS_INPUT: [
                CommandHandler("cancel", cmd_cancel),
                CommandHandler("help",   cmd_help),
                CallbackQueryHandler(handle_no_extra_skills, pattern="^no_extra_skills$"),
                MessageHandler(filters.VOICE, handle_clarification_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_skills_input),
            ],
            WAITING_UPDATE_CONFIRM: [
                CallbackQueryHandler(handle_update_confirm_go,   pattern="^update_confirm_go$"),
                CallbackQueryHandler(handle_update_confirm_more, pattern="^update_confirm_more$"),
                CommandHandler("cancel", cmd_cancel),
                CommandHandler("help",   cmd_help),
                MessageHandler(filters.VOICE, handle_update_confirm_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_update_additional_text),
            ],
            WAITING_ATS_CONFIRM: [
                CallbackQueryHandler(handle_ats_yes,  pattern="^ats_yes$"),
                CallbackQueryHandler(handle_ats_skip, pattern="^ats_skip$"),
                CommandHandler("cancel", cmd_cancel),
                CommandHandler("help",   cmd_help),
            ],
            **apply_conv_states,
        },
        fallbacks=[
            CommandHandler("start",     cmd_start),
            CommandHandler("new",       cmd_new),
            CommandHandler("cancel",    cmd_cancel),
            CommandHandler("help",      cmd_help),
            CommandHandler("my_resume", cmd_my_resume),
        ],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("my_resume", cmd_my_resume))
    app.add_handler(CallbackQueryHandler(handle_download_resume,      pattern="^dl_resume_"))
    app.add_handler(CallbackQueryHandler(handle_update_confirm_go,    pattern="^update_confirm_go$"))
    app.add_handler(CallbackQueryHandler(handle_update_confirm_more,  pattern="^update_confirm_more$"))
    app.add_handler(CallbackQueryHandler(handle_skip_question,        pattern="^skip_question$"))
    app.add_handler(CallbackQueryHandler(handle_no_extra_skills,      pattern="^no_extra_skills$"))
    app.add_handler(CallbackQueryHandler(handle_ats_yes,              pattern="^ats_yes$"))
    app.add_handler(CallbackQueryHandler(handle_ats_skip,             pattern="^ats_skip$"))
    app.add_handler(CallbackQueryHandler(handle_action_update_resume, pattern="^action_update_resume$"))
    app.add_handler(CallbackQueryHandler(handle_action_find_job,      pattern="^action_find_job$"))
    app.add_handler(CallbackQueryHandler(handle_action_job_link,      pattern="^action_job_link$"))
    app.add_handler(CallbackQueryHandler(handle_resume_use_saved,     pattern="^resume_use_saved$"))
    app.add_handler(CallbackQueryHandler(handle_resume_view_saved,    pattern="^resume_view_saved$"))
    app.add_handler(CallbackQueryHandler(handle_resume_upload_new,    pattern="^resume_upload_new$"))
    app.add_handler(CallbackQueryHandler(handle_jp_role,              pattern="^jp_role_"))
    app.add_handler(CallbackQueryHandler(handle_jp_city,              pattern="^jp_city_"))
    app.add_handler(CallbackQueryHandler(handle_jp_exp,               pattern="^jp_exp_"))
    app.add_handler(CallbackQueryHandler(handle_jp_time,              pattern="^jp_time_"))
    app.add_handler(CallbackQueryHandler(handle_optimize_job,         pattern="^optimize_"))
    # ── Apply callbacks — registered globally so they work after conversation ends ──
    for h in apply_global_handlers:
        app.add_handler(h)
    for h in apply_commands:
        app.add_handler(h)
    app.add_handler(CallbackQueryHandler(handle_expired_buttons))
    app.add_handler(MessageHandler(filters.ALL, handle_fallback))

    logger.info("Bot is running... Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)
if __name__ == "__main__":
    main()