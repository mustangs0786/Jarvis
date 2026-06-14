"""
apply_skills/resume_skill.py
Handles resume/CV upload steps.
Supports:
  1. Portals with "From Device" / "Upload" button that opens a file dialog
  2. Portals with a direct file input
  3. Gemini fallback for anything else
"""

import logging
from pathlib import Path
from .base import parse_gemini_json, run_actions, click_text_in_frames, upload_in_frames, json_config

logger = logging.getLogger(__name__)

# Buttons that open a file chooser dialog
DEVICE_BUTTON_TEXTS = [
    "From Device", "Upload from Device", "Upload from Computer",
    "Upload Resume", "Upload CV", "Choose File", "Browse",
    "Upload a file", "Select file", "Upload file",
]

PROMPT = """You are handling a resume/CV upload step in a job application.

Look at the page HTML and return actions to upload a resume.

If you see buttons like "From Device", "Upload from Computer", "Browse" that open a file chooser:
  Return a click action on that button.

If you see a visible file input:
  Return an upload action on it.

Return a JSON array of actions:
- Click action: {{"action": "click", "selector": "css selector", "value": null, "label": "From Device button"}}
- Upload action: {{"action": "upload", "selector": "input[type='file']", "value": "__RESUME__", "label": "Resume"}}

Rules:
- Prefer the "From Device" / upload button approach if both exist
- For cover letter inputs: set value to null (skip)
- Return ONLY a valid JSON array, no markdown

Page HTML:
{html}
"""


CONFIRM_TEXTS = [
    "Use this resume", "Use resume", "Use this CV", "Use CV",
    "Upload", "Confirm", "Continue", "Next", "Done",
    "Apply with this resume", "Proceed",
]

async def _click_upload_confirm(page) -> bool:
    """After upload, click any confirmation/proceed button that appears.
    Searches all frames. Waits up to 4 seconds for server-side upload processing."""
    for wait_ms in (1200, 1500, 1300):  # total ~4 seconds of waiting
        await page.wait_for_timeout(wait_ms)
        ok, txt = await click_text_in_frames(
            page, CONFIRM_TEXTS,
            tags=("button", "a", "div[role='button']", "span[role='button']"),
        )
        if ok:
            logger.info(f"  ResumeSkill confirm: clicked '{txt}'")
            await page.wait_for_timeout(1500)
            return True
    logger.info("  ResumeSkill: no confirm button found after upload")
    return False


async def _try_file_chooser(page, btn_locator, resume_path: str) -> bool:
    """Click a button that opens a file chooser, then set the file."""
    try:
        async with page.expect_file_chooser(timeout=5000) as fc_info:
            await btn_locator.click()
        file_chooser = await fc_info.value
        await file_chooser.set_files(resume_path)
        logger.info(f"  ResumeSkill file chooser: {Path(resume_path).name}")
        await page.wait_for_timeout(1500)
        await _click_upload_confirm(page)
        return True
    except Exception as e:
        logger.debug(f"  File chooser attempt failed: {e}")
        return False


async def run(page, profile: dict, resume_path: str,
              gemini_client, model: str, on_stuck=None, user_id: int = None) -> tuple[list, list]:

    if not resume_path or not Path(resume_path).exists():
        logger.warning("  ResumeSkill: resume_path not found")
        return [], ["no_resume"]

    # ── Step 1: Try "From Device" / upload button across all frames ──────────
    for frame in page.frames:
        for txt in DEVICE_BUTTON_TEXTS:
            for tag in ("button", "a", "span", "div"):
                try:
                    el = frame.locator(f"{tag}:has-text('{txt}')").first
                    if await el.count() > 0 and await el.is_visible():
                        if await _try_file_chooser(page, el, resume_path):
                            return ["Resume"], []
                except Exception:
                    continue

    # ── Step 2: Direct file input across all frames ───────────────────────────
    if await upload_in_frames(page, resume_path):
        logger.info(f"  ResumeSkill frame upload: {Path(resume_path).name}")
        await page.wait_for_timeout(1200)
        await _click_upload_confirm(page)
        return ["Resume"], []

    # ── Step 3: Gemini fallback ───────────────────────────────────────────────
    try:
        html_parts = []
        for frame in page.frames:
            try:
                html_parts.append(await frame.inner_html("body"))
            except Exception:
                pass
        html = "\n".join(html_parts)[:6000]
        response = gemini_client.models.generate_content(
            model=model,
            contents=PROMPT.format(html=html),
            config=json_config(),
        )
        actions = parse_gemini_json(response.text or "[]")
        if not isinstance(actions, list) or not actions:
            return [], []

        # If Gemini suggests clicking a button (to open file dialog), try file chooser
        for action in actions:
            if action.get("action") == "click":
                sel = action.get("selector", "")
                if sel:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0 and await el.is_visible():
                            if await _try_file_chooser(page, el, resume_path):
                                return ["Resume"], []
                    except Exception:
                        pass

        logger.info(f"  ResumeSkill Gemini: {len(actions)} actions")
        return await run_actions(page, actions, resume_path, on_stuck, user_id)
    except Exception as e:
        logger.warning(f"  ResumeSkill failed: {e}")
        return [], []
