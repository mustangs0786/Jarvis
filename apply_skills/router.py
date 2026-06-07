"""
apply_skills/router.py — Page classifier.
Looks at screenshot + HTML and decides which skills to invoke.
Uses flash (fast, cheap) — just classification, no form filling.
"""

import logging
import base64
from .base import parse_gemini_json, json_config

logger = logging.getLogger(__name__)

ROUTER_PROMPT = """You are analyzing a job application webpage.
Look at the screenshot and HTML carefully.

Classify this page and return a JSON object:
{{
  "page_type": one of: "landing" | "account" | "resume_upload" | "contact_form" | "screening" | "review" | "submitted" | "error" | "unknown",
  "skills":    list of skills needed for this page (subset of: ["account", "resume", "contact", "screening", "review"]),
  "has_apply_button": true/false,
  "is_submitted": true/false,
  "is_login_required": true/false,
  "has_guest_option": true/false,
  "notes": "brief description of what you see"
}}

Page type meanings:
- landing: job description page with an Apply button — need to click Apply first
- account: login / register / sign in form
- resume_upload: page asking to upload a CV/resume file
- contact_form: form with name, email, phone, location, LinkedIn fields
- screening: custom questions (years of experience, salary, availability, etc.)
- review: summary/review page before final submission
- submitted: application already submitted successfully
- error: error page or something went wrong
- unknown: cannot determine

IMPORTANT: A single page may need multiple skills (e.g. contact + screening on same page).
Return ONLY valid JSON, no markdown.

HTML (truncated):
{html}
"""

async def route_page(page, gemini_client, model: str) -> dict:
    """
    Analyze current page with screenshot + HTML.
    Returns classification dict with 'skills' list.
    """
    default = {
        "page_type": "unknown",
        "skills": ["contact"],
        "has_apply_button": False,
        "is_submitted": False,
        "is_login_required": False,
        "has_guest_option": False,
        "notes": "",
    }

    try:
        # Screenshot for visual context
        ss_bytes = await page.screenshot(full_page=False)
        img_b64  = base64.b64encode(ss_bytes).decode()

        # Collect HTML from all frames — handles portals with embedded iframes
        html_parts = []
        for frame in page.frames:
            try:
                html_parts.append(await frame.inner_html("body"))
            except Exception:
                pass
        html = "\n".join(html_parts)[:6000]

        prompt = ROUTER_PROMPT.format(html=html)

        response = gemini_client.models.generate_content(
            model=model,
            contents=[{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": img_b64}},
            ]}],
            config=json_config(),
        )
        result = parse_gemini_json(response.text or "{}")
        logger.info(f"  Router: {result.get('page_type')} | skills={result.get('skills')} | notes={result.get('notes','')[:60]}")
        return {**default, **result}

    except Exception as e:
        logger.warning(f"  Router failed: {e}")
        return default
