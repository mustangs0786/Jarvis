"""
apply_skills/review_skill.py
Handles review/summary pages before final submission.
Checks for any missed fields or errors on the review page.
"""

import json
import logging
from .base import parse_gemini_json, run_actions, json_config

logger = logging.getLogger(__name__)

PROMPT = """You are on a review/summary page of a job application.

Look at the page carefully:
1. Are there any empty required fields that need to be filled?
2. Are there any validation errors shown?
3. Is there an "Edit" section that needs attention?

If everything looks complete, return an empty array [].
If there are issues to fix, return actions to fix them.

Return a JSON array of browser actions (or [] if nothing to fix).
Each action: {{"action": "...", "selector": "...", "value": "...", "label": "..."}}

Return ONLY a valid JSON array, no markdown.

Profile:
{profile}

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
        html = "\n".join(html_parts)[:8000]
        safe = {k: v for k, v in profile.items()
                if k not in ("password", "_resume_text") and v}

        response = gemini_client.models.generate_content(
            model=model,
            contents=PROMPT.format(
                profile=json.dumps(safe, indent=2),
                html=html,
            ),
            config=json_config(),
        )
        actions = parse_gemini_json(response.text or "[]")
        if not isinstance(actions, list) or not actions:
            logger.info("  ReviewSkill: nothing to fix")
            return [], []
        logger.info(f"  ReviewSkill: {len(actions)} fix actions")
        return await run_actions(page, actions, resume_path, on_stuck, user_id)
    except Exception as e:
        logger.warning(f"  ReviewSkill failed: {e}")
        return [], []
