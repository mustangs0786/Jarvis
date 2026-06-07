"""
external_apply.py — Skill-based external job portal automation
==============================================================
Handles any job application portal (Greenhouse, Lever, Ashby, Workday, custom).
Completely separate from linkedin_easy_apply.py — no shared imports.

Flow:
  1. Open URL in browser
  2. Router analyzes page → decides which skills to load
  3. Skills execute focused Gemini prompts → actions → dispatcher runs them
  4. Human-in-loop review after each step
  5. Final submit confirmation

Usage (CLI):
  python external_apply.py <job_url> [resume_pdf] [--live]
"""

import os
import re
import json
import asyncio
import logging
import base64
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
load_dotenv()

from profile_manager import load_profile, learn_answer, get_field_value
from job_wiki import get_portal_knowledge, save_portal_knowledge

logger = logging.getLogger(__name__)

MAX_STEPS   = 15
FLASH_MODEL = "gemini-3.5-flash"
PRO_MODEL   = "gemini-3.5-flash"

SUBMIT_KEYWORDS = [
    "application submitted", "thank you for applying",
    "successfully submitted", "application received",
    "we have received your application", "you've applied",
    "application complete", "your application has been",
]

# ── Skill registry — loaded on demand ────────────────────────────────────────

SKILL_MAP = {
    "account":   "apply_skills.account_skill",
    "resume":    "apply_skills.resume_skill",
    "contact":   "apply_skills.contact_skill",
    "screening": "apply_skills.screening_skill",
    "review":    "apply_skills.review_skill",
}

def load_skill(name: str):
    """Import and return a skill module on demand."""
    import importlib
    module_path = SKILL_MAP.get(name)
    if not module_path:
        logger.warning(f"  Unknown skill: {name}")
        return None
    try:
        return importlib.import_module(module_path)
    except Exception as e:
        logger.warning(f"  Failed to load skill '{name}': {e}")
        return None


# ── Result ────────────────────────────────────────────────────────────────────

class ExternalApplyResult:
    def __init__(self):
        self.status          = "pending"
        self.portal          = "external"
        self.job_title       = ""
        self.company         = ""
        self.fields_filled   = []
        self.fields_skipped  = []
        self.fields_learned  = []
        self.screenshot_path = ""
        self.steps_completed = 0
        self.error           = ""


# ── Navigation helpers ────────────────────────────────────────────────────────

async def click_apply_button(page, gemini_client=None, model: str = FLASH_MODEL) -> tuple[bool, object]:
    """Click Apply / Apply Now button on job landing page.
    Returns (clicked, new_page_or_none) — if a new tab opened, returns the new page.
    Falls back to Gemini if hardcoded selectors fail."""
    from apply_skills.base import click_text_in_frames

    APPLY_TEXTS = ["Apply Now", "Apply now", "Apply", "Apply for this job",
                   "Apply for this position", "Apply Here", "Apply for Job"]

    async def _click_and_detect_newtab(click_fn) -> tuple[bool, object]:
        """Run a click function, detect if a new tab opens. Returns (True, new_page) or (True, None)."""
        try:
            async with page.context.expect_page(timeout=3000) as new_page_info:
                await click_fn()
            new_pg = await new_page_info.value
            await new_pg.wait_for_load_state("domcontentloaded", timeout=10000)
            logger.info(f"  Apply opened new tab: {new_pg.url[:80]}")
            return True, new_pg
        except Exception:
            # No new tab — may have navigated same page
            return True, None

    # Try standard texts via frame search
    for text in APPLY_TEXTS:
        for tag in ("button", "a"):
            for frame in page.frames:
                try:
                    els = frame.locator(f"{tag}:has-text('{text}')")
                    count = await els.count()
                    for i in range(count):
                        el = els.nth(i)
                        if await el.is_visible():
                            ok, new_pg = await _click_and_detect_newtab(lambda e=el: e.click(force=True))
                            if ok:
                                logger.info(f"  Clicked apply: '{text}'")
                                await page.wait_for_timeout(1500)
                                return True, new_pg
                except Exception:
                    continue

    # Gemini fallback
    if gemini_client:
        try:
            await page.wait_for_timeout(2000)
            html_parts = []
            for frame in page.frames:
                try:
                    html_parts.append(await frame.inner_html("body"))
                except Exception:
                    pass
            html = "\n".join(html_parts)[:5000]
            prompt = f"""Look at this job page HTML and find the button or link to APPLY for the job.

Return JSON: {{"selector": "css selector", "text": "button text"}}
If no apply button found, return {{"selector": null, "text": null}}
Return ONLY valid JSON, no markdown.

HTML: {html}"""
            resp = gemini_client.models.generate_content(model=model, contents=prompt)
            raw  = (resp.text or "").strip()
            raw  = re.sub(r"^```(?:json)?\s*", "", raw)
            raw  = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
            sel  = data.get("selector")
            if sel:
                for frame in page.frames:
                    try:
                        els = frame.locator(sel)
                        cnt = await els.count()
                        for i in range(cnt):
                            el = els.nth(i)
                            if await el.is_visible():
                                ok, new_pg = await _click_and_detect_newtab(lambda e=el: e.click(force=True))
                                if ok:
                                    logger.info(f"  Gemini apply: {sel}")
                                    await page.wait_for_timeout(1500)
                                    return True, new_pg
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"  Gemini apply fallback: {e}")

    return False, None


async def click_next_or_submit(page, gemini_client=None, model: str = FLASH_MODEL) -> str:
    """Click Next / Continue / Submit across all frames. Returns action label."""
    from apply_skills.base import click_text_in_frames

    SUBMIT_TEXTS = [
        "Submit application", "Submit Application", "Submit",
        "Complete Application", "Finish",
    ]
    REVIEW_TEXTS = ["Review", "Review Application"]
    NEXT_TEXTS = [
        "Next", "Continue", "Next Step",
        "Save and Continue", "Save & Continue",
        "Next >", "Continue >", "Proceed",
        "Go to next step", "Upload and Continue", "Upload & Continue",
        "Save",
    ]

    for texts, label in [
        (SUBMIT_TEXTS, "submit"),
        (REVIEW_TEXTS, "review"),
        (NEXT_TEXTS,   "next"),
    ]:
        ok, matched = await click_text_in_frames(
            page, texts,
            tags=("button", "a", "input[type='submit']"),
        )
        if ok:
            await page.wait_for_timeout(1500)
            return label

    # Try generic input[type=submit] in any frame
    for frame in page.frames:
        try:
            el = frame.locator("input[type='submit']").first
            if await el.count() > 0 and await el.is_visible():
                val = (await el.get_attribute("value") or "").lower()
                lbl = "submit" if any(w in val for w in ("submit", "finish", "complete")) else "next"
                await el.click(timeout=5000)
                logger.info(f"  Clicked input[type=submit]: {val}")
                await page.wait_for_timeout(1500)
                return lbl
        except Exception:
            continue

    # Gemini fallback — collect HTML from all frames and ask for nav button
    if gemini_client:
        try:
            html_parts = []
            for frame in page.frames:
                try:
                    html_parts.append(await frame.inner_html("body"))
                except Exception:
                    pass
            combined_html = "\n".join(html_parts)[:6000]
            prompt = f"""Look at this job application page HTML and find the button/link to go to the NEXT step or SUBMIT the form.

Return JSON: {{"selector": "css selector", "label": "next|submit|review"}}
If there is no navigation button visible, return {{"selector": null, "label": "none"}}
Return ONLY valid JSON, no markdown.

HTML: {combined_html}"""
            resp = gemini_client.models.generate_content(model=model, contents=prompt)
            raw = (resp.text or "").strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
            sel   = data.get("selector")
            label = data.get("label", "next")
            if sel and label != "none":
                for frame in page.frames:
                    try:
                        el = frame.locator(sel).first
                        if await el.count() > 0 and await el.is_visible():
                            await el.click(timeout=5000)
                            logger.info(f"  Gemini nav: {sel} [frame={frame.url[:60]}]")
                            await page.wait_for_timeout(1500)
                            return label
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"  Gemini nav fallback failed: {e}")

    return "none"


async def is_submitted(page) -> bool:
    try:
        body = (await page.inner_text("body") or "").lower()
        return any(kw in body for kw in SUBMIT_KEYWORDS)
    except Exception:
        return False


# ── Fix instruction handler ───────────────────────────────────────────────────

async def apply_fix(page, instruction: str, profile: dict,
                    gemini_client, model: str):
    """User described a fix — ask Gemini to plan one action and execute it."""
    from apply_skills.base import dispatch_action, parse_gemini_json
    try:
        html = (await page.inner_html("body"))[:6000]
        safe = {k: v for k, v in profile.items() if k not in ("password", "_resume_text") and v}
        prompt = f"""A job application form is open. The user wants to fix: "{instruction}"

Profile: {json.dumps(safe, indent=2)}

HTML: {html}

Return ONE browser action as JSON to fix exactly what the user described:
{{"action": "fill|click|click_option|clear_and_fill|press_sequentially", "selector": "css selector", "value": "new value", "label": "field name"}}
Return ONLY the JSON object, no markdown."""
        response = gemini_client.models.generate_content(model=model, contents=prompt)
        raw = (response.text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        action = json.loads(raw)
        await dispatch_action(page, action, "", None, set())
        await page.wait_for_timeout(600)
    except Exception as e:
        logger.warning(f"  Fix failed: {e}")


# ── Screenshot helper ─────────────────────────────────────────────────────────

async def _check_consent_boxes(page) -> int:
    """Before submit: find and check any unchecked consent/terms/agreement checkboxes.
    Returns number of checkboxes checked."""
    checked = 0
    total_found = 0
    for fi, frame in enumerate(page.frames):
        try:
            boxes = frame.locator("input[type='checkbox']")
            count = await boxes.count()
            total_found += count
            for i in range(count):
                box = boxes.nth(i)
                try:
                    vis = await box.is_visible()
                    chk = await box.is_checked()
                    logger.info(f"  Consent box frame={fi} [{i}]: visible={vis} checked={chk}")
                    if vis and not chk:
                        # Scroll to checkbox and use keyboard Space (works for iCheck/custom checkboxes)
                        try:
                            await box.scroll_into_view_if_needed()
                            await page.wait_for_timeout(300)
                            await box.focus()
                            await page.keyboard.press("Space")
                            await page.wait_for_timeout(300)
                        except Exception:
                            pass

                        # Fallback: click the parent label
                        if not await box.is_checked():
                            try:
                                lbl = frame.locator(f"label:has(input[type='checkbox'])")
                                cnt = await lbl.count()
                                for li in range(cnt):
                                    l = lbl.nth(li)
                                    if await l.is_visible():
                                        await l.scroll_into_view_if_needed()
                                        await l.click(force=True, timeout=2000)
                                        break
                            except Exception:
                                pass

                        await page.wait_for_timeout(400)
                        try:
                            if await box.is_checked():
                                logger.info(f"  Checked consent checkbox frame={fi} [{i}]")
                                checked += 1
                            else:
                                logger.warning(f"  Still unchecked after attempts frame={fi} [{i}]")
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"  Consent box error frame={fi} [{i}]: {e}")
                    continue
        except Exception as e:
            logger.debug(f"  _check_consent_boxes frame error: {e}")
            continue
    logger.info(f"  _check_consent_boxes: found={total_found} checked={checked}")
    return checked


async def take_screenshot(page, user_id: int, step: int,
                           on_screenshot: Callable = None) -> str:
    try:
        ss = f"output/ext_{user_id}_step{step}.png"
        await page.screenshot(path=ss, full_page=False)
        if on_screenshot:
            await on_screenshot(ss)
        return ss
    except Exception:
        return ""


# ── Main entry ────────────────────────────────────────────────────────────────

async def run_external_apply(
    job_url:       str,
    resume_path:   str,
    user_id:       int,
    gemini_client  = None,
    model:         str = FLASH_MODEL,
    pro_model:     str = PRO_MODEL,
    on_stuck:      Callable = None,
    on_screenshot: Callable = None,
    on_notify:     Callable = None,
) -> ExternalApplyResult:

    from playwright.async_api import async_playwright
    from apply_skills.router import route_page

    profile     = load_profile(user_id)
    result      = ExternalApplyResult()
    Path("output").mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await ctx.new_page()

        try:
            logger.info(f"  Loading: {job_url}")
            await page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # Extract title/company from page
            try:
                t = await page.title()
                result.job_title = t.split("|")[0].split("–")[0].strip()[:80]
                result.company   = (t.split("|")[-1].strip() if "|" in t else "")[:60]
            except Exception:
                pass

            if on_notify:
                await on_notify(
                    f"🌐 *Loaded:* {result.job_title or job_url[:60]}\n"
                    f"🏢 *Company:* {result.company or 'unknown'}\n\n"
                    f"Analyzing page..."
                )

            await take_screenshot(page, user_id, 0, on_screenshot)

            # ── Portal playbook: have we been here before? ────────────────
            known = get_portal_knowledge(page.url)
            if known and on_notify:
                bits = []
                if known.get("account_created"):
                    bits.append("account on file")
                if known.get("last_status"):
                    bits.append(f"last attempt: {known['last_status']}")
                if bits:
                    await on_notify(f"📒 *Seen this portal before* — {', '.join(bits)}.")

            step_num        = 0
            prev_url        = ""
            stuck_count     = 0
            prev_page_type  = ""
            page_type_count = 0  # consecutive same page_type counter

            while step_num < MAX_STEPS:
                await page.wait_for_timeout(1500)

                # Check if already submitted
                if await is_submitted(page):
                    logger.info("  Submission confirmed!")
                    result.status          = "success"
                    result.steps_completed = step_num
                    if on_notify:
                        await on_notify("✅ *Application submitted successfully!*")
                    break

                step_num += 1
                logger.info(f"\n  ── Step {step_num} ──")

                # ── Router: classify page ─────────────────────────────────
                classification = await route_page(page, gemini_client, model)
                page_type = classification.get("page_type", "unknown")
                skills    = classification.get("skills", ["contact"])

                # Loop detector — same page type 3 times in a row → ask user
                if page_type == prev_page_type:
                    page_type_count += 1
                else:
                    page_type_count = 1
                    prev_page_type  = page_type

                if page_type_count >= 3 and on_stuck:
                    reply = (await on_stuck(
                        f"Stuck on '{page_type}' for {page_type_count} steps. "
                        "Describe what to fix, or reply 'skip' to try Continue anyway, or 'cancel' to stop."
                    ) or "").strip().lower()
                    if reply == "cancel":
                        result.status = "cancelled"
                        break
                    if reply not in ("skip", "ok", ""):
                        await apply_fix(page, reply, profile, gemini_client, pro_model or model)
                        page_type_count = 0  # reset after user fix

                if on_notify:
                    await on_notify(
                        f"📋 *Step {step_num}:* {page_type}\n"
                        f"🔧 Skills: {', '.join(skills)}"
                    )

                # ── Handle submitted page ─────────────────────────────────
                if page_type == "submitted" or classification.get("is_submitted"):
                    result.status          = "success"
                    result.steps_completed = step_num
                    if on_notify:
                        await on_notify("✅ *Application submitted successfully!*")
                    break

                # ── Handle landing page — click Apply button ──────────────
                if page_type == "landing" or classification.get("has_apply_button"):
                    logger.info("  Landing page — clicking Apply")
                    if on_notify:
                        await on_notify("🖱️ Found job landing page — clicking Apply...")
                    clicked, new_pg = await click_apply_button(page, gemini_client, model)
                    if new_pg:
                        # Apply opened a new tab — switch to it
                        logger.info("  Switched to new apply tab")
                        page = new_pg
                        prev_url = page.url
                        page_type_count = 0
                    elif not clicked:
                        if on_stuck:
                            await on_stuck("Could not find Apply button. Please click it manually, then reply 'ok'.")
                    await page.wait_for_timeout(2000)
                    await take_screenshot(page, user_id, step_num, on_screenshot)
                    continue

                # ── Execute skills ────────────────────────────────────────
                url_before_skills = page.url
                step_filled  = []
                step_skipped = []

                for skill_name in skills:
                    # pro_model may be empty when the caller didn't set one — fall
                    # back to the base model so skills never get an empty model string.
                    skill_model = (pro_model or model) if skill_name in ("account", "screening") else model
                    skill = load_skill(skill_name)
                    if not skill:
                        continue
                    logger.info(f"  Running skill: {skill_name}")
                    filled, skipped = await skill.run(
                        page=page,
                        profile=profile,
                        resume_path=resume_path,
                        gemini_client=gemini_client,
                        model=skill_model,
                        on_stuck=on_stuck,
                        user_id=user_id,
                    )
                    step_filled  += filled
                    step_skipped += skipped
                    result.fields_filled  += filled
                    result.fields_skipped += skipped

                if step_filled and on_notify:
                    filled_txt = "\n".join(f"  ✅ {f}" for f in step_filled[:10])
                    await on_notify(f"📝 *Step {step_num} filled:*\n{filled_txt}")

                # ── Check if skills already navigated to a new page ───────
                url_after_skills = page.url
                if url_after_skills != url_before_skills:
                    # Skills changed the URL — skip click_next_or_submit this step
                    logger.info(f"  Skill navigated to: {url_after_skills[:80]}")
                    prev_url = url_after_skills
                    page_type_count = 0  # reset loop detector
                    await page.wait_for_timeout(1500)
                    await take_screenshot(page, user_id, step_num, on_screenshot)
                    continue

                # ── Screenshot (streamed live to the user) ────────────────
                # Run autonomously — no per-step confirmation. The user only
                # gets pulled in when we're genuinely stuck (no nav button) or
                # right before the final submit.
                await take_screenshot(page, user_id, step_num, on_screenshot)

                # ── Auto-check consent/terms before navigating ────────────
                await _check_consent_boxes(page)

                # ── Navigate to next step ─────────────────────────────────
                action = await click_next_or_submit(page, gemini_client, model)
                await page.wait_for_timeout(2000)

                if action == "submit":
                    # Final submit confirmation
                    await take_screenshot(page, user_id, step_num, on_screenshot)
                    if on_stuck:
                        confirm = (await on_stuck(
                            "Ready to submit? Reply *submit* to apply now, or *cancel* to stop."
                        ) or "").strip().lower()
                        if confirm not in ("submit", "yes", "y", "ok"):
                            result.status          = "cancelled"
                            result.steps_completed = step_num
                            if on_notify:
                                await on_notify("❌ Application cancelled.")
                            break

                    await page.wait_for_timeout(5000)
                    # Check for submission confirmation
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    if await is_submitted(page):
                        result.status = "success"
                    else:
                        # Take a screenshot so user can verify
                        await take_screenshot(page, user_id, step_num + 1, on_screenshot)
                        # Assume success if no error detected (many portals don't have standard confirmation text)
                        body = (await page.inner_text("body") or "").lower()
                        if not any(err in body for err in ("error", "required", "invalid", "failed")):
                            logger.info("  No confirmation text but no errors — assuming success")
                            result.status = "success"
                        else:
                            result.status = "failed"
                            result.error  = "Submit clicked but confirmation not detected"
                    result.steps_completed = step_num
                    break

                elif action == "none":
                    stuck_count += 1
                    logger.warning(f"  No Next/Submit button (stuck_count={stuck_count})")

                    # Recovery: we may be on a job description/landing page whose
                    # Apply button was never clicked. Try it before giving up.
                    clicked, new_pg = await click_apply_button(page, gemini_client, model)
                    if new_pg:
                        page = new_pg
                        prev_url = page.url
                        page_type_count = 0
                        stuck_count = 0
                        await page.wait_for_timeout(1500)
                        await take_screenshot(page, user_id, step_num, on_screenshot)
                        continue
                    if clicked and page.url != prev_url:
                        prev_url = page.url
                        page_type_count = 0
                        stuck_count = 0
                        await page.wait_for_timeout(1500)
                        continue

                    if stuck_count >= 2:
                        if on_stuck:
                            reply = (await on_stuck(
                                "Stuck — I can't find a Next / Submit / Apply button on this page. "
                                "Tell me what to do (e.g. 'click Apply', 'fill the email field'), "
                                "or reply 'retry' to try again, or 'cancel' to stop."
                            ) or "").strip().lower()
                            if "cancel" in reply:
                                result.status = "cancelled"
                                break
                            if reply and reply not in ("retry", "ok", "skip", "yes", ""):
                                await apply_fix(page, reply, profile, gemini_client, pro_model or model)
                        stuck_count = 0
                else:
                    stuck_count = 0

                # Detect if URL hasn't changed (no progress)
                current_url = page.url
                if current_url == prev_url and action == "none":
                    logger.info("  No URL change — may be stuck")
                prev_url = current_url

            # Final status
            if result.status == "pending":
                result.status = "failed"
                result.error  = f"Completed {step_num} steps without submit confirmation"
                result.steps_completed = step_num

            # ── Portal playbook: remember this run for next time ──────────
            try:
                save_portal_knowledge(page.url, {
                    "portal":       result.portal,
                    "last_status":  result.status,
                    "last_steps":   result.steps_completed,
                    "job_title":    result.job_title,
                    "company":      result.company,
                })
            except Exception:
                pass

            # Final screenshot
            await take_screenshot(page, user_id, step_num + 1, on_screenshot)

        except Exception as e:
            result.status = "failed"
            result.error  = str(e)
            logger.error(f"  External apply error: {e}")
            try:
                ss = f"output/ext_{user_id}_error.png"
                await page.screenshot(path=ss)
                result.screenshot_path = ss
                if on_screenshot:
                    await on_screenshot(ss)
            except Exception:
                pass
        finally:
            await browser.close()

    logger.info(
        f"  Result: {result.status} | Steps: {result.steps_completed} | "
        f"Filled: {len(result.fields_filled)}"
    )
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    _profile_dir   = Path(r"D:\Projects\Resume_Builder\user_profiles\917484502")
    _pdfs          = sorted(_profile_dir.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)
    DEFAULT_RESUME = str(_pdfs[0]) if _pdfs else ""

    if len(sys.argv) < 2:
        print("Usage: python external_apply.py <job_url> [resume_pdf] [--live]")
        sys.exit(1)

    job_url     = sys.argv[1]
    resume_path = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else DEFAULT_RESUME
    USER_ID     = 917484502

    async def _on_stuck(q):
        try:
            return input(f"\n  ❓ {q}\n  Answer: ").strip()
        except EOFError:
            return "ok"

    async def _on_screenshot(path):
        print(f"  Screenshot → {path}")

    async def _on_notify(msg):
        # strip markdown for terminal
        print(f"  {msg.replace('*','').replace('_','')}")

    from google import genai as _genai
    _gemini    = _genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
    _model     = os.getenv("GEMINI_MODEL",     FLASH_MODEL)
    _pro_model = os.getenv("GEMINI_PRO_MODEL", PRO_MODEL)

    print(f"\nExternal Apply\n  URL    : {job_url}\n  Resume : {resume_path}\n")

    result = asyncio.run(run_external_apply(
        job_url=job_url, resume_path=resume_path, user_id=USER_ID,
        gemini_client=_gemini, model=_model, pro_model=_pro_model,
        on_stuck=_on_stuck, on_screenshot=_on_screenshot, on_notify=_on_notify,
    ))

    print(f"\nStatus  : {result.status.upper()}")
    print(f"Steps   : {result.steps_completed}")
    print(f"Filled  : {result.fields_filled}")
    print(f"Skipped : {result.fields_skipped}")
    if result.error:
        print(f"Error   : {result.error}")
