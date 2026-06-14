"""
apply_skills/screening_skill.py
Handles custom screening questions: years of exp, salary, notice period,
availability, specific skills, custom dropdowns, radio yes/no questions.
Uses pro model — answers require judgment from profile + resume context.
"""

import json
import logging
from .base import parse_gemini_json, run_actions, json_config

logger = logging.getLogger(__name__)

PROMPT = """You are answering custom screening questions in a job application form.

You will receive the applicant's full profile and resume text.
Your job: identify every screening question on this page and return the best answer for each.

Return a JSON array of browser actions.

Each action:
- "action": fill | click | click_option | clear_and_fill | press_sequentially | click (for radio/checkbox labels)
- "selector": best CSS selector
- "value": answer — MUST come from profile or resume. NEVER invent.
- "label": the question text exactly as shown

Rules for specific question types:
- Years of experience (numeric): use standard rounding (5.7→6, 6.3→6). Never truncate.
- Skills NOT in resume: return null — do not guess 0 or any number
- Current CTC / salary: use profile "current_ctc" field
- Expected CTC / salary: use profile "expected_ctc" field
- Notice period: use profile "notice_period" field
- Willing to relocate: use profile "willing_to_relocate" field
- Visa / sponsorship / work authorization: applicant is Indian citizen in India → "No" / "I don't require sponsorship"
- Radio buttons: click the <label> element, NOT the hidden <input>
- Checkboxes for "I agree / follow": set value to null (skip)
- For select/combobox: use click_option with option text as value
- Return null for anything genuinely not answerable from profile/resume

Profile:
{profile}

Resume text:
{resume_text}

Page HTML:
{html}
"""

async def run(page, profile: dict, resume_path: str,
              gemini_client, model: str, on_stuck=None, user_id: int = None) -> tuple[list, list]:
    try:
        html_parts = []
        for frame in page.frames:
            try:
                html_parts.append(await frame.inner_html("body"))
            except Exception:
                pass
        html         = "\n".join(html_parts)[:12000]
        resume_text  = profile.get("_resume_text", "")[:4000]
        safe         = {k: v for k, v in profile.items()
                        if k not in ("password", "_resume_text") and v}

        response = gemini_client.models.generate_content(
            model=model,
            contents=PROMPT.format(
                profile=json.dumps(safe, indent=2),
                resume_text=resume_text,
                html=html,
            ),
            config=json_config(),
        )
        actions = parse_gemini_json(response.text or "[]")
        if not isinstance(actions, list):
            return [], []
        logger.info(f"  ScreeningSkill: {len(actions)} actions")
        return await run_actions(page, actions, resume_path, on_stuck, user_id)
    except Exception as e:
        logger.warning(f"  ScreeningSkill failed: {e}")
        return [], []
