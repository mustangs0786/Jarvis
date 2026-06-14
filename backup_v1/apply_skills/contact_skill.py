"""
apply_skills/contact_skill.py
Handles standard contact info fields: name, email, phone, location, LinkedIn, etc.
"""

import json
import logging
from .base import parse_gemini_json, run_actions, json_config

logger = logging.getLogger(__name__)

PROMPT = """You are filling standard contact information fields in a job application form.

Return a JSON array of browser actions to fill every visible contact field on this page.

Each action:
- "action": fill | click | click_option | clear_and_fill | press_sequentially | upload
- "selector": best CSS selector (#id > [name='x'] > [aria-label='x'] > visible text)
- "value": exact value from profile — NEVER invent. Use null if not in profile.
- "label": human-readable field name

Fields to look for and fill:
- First name, Last name, Full name
- Email address
- Phone / mobile number — use profile "phone_full" (e.g. +919780616787) for single phone fields, or "phone_country_code" + "phone" if separate fields
- Phone country code
- City, Location, Country
- LinkedIn URL
- Portfolio / Website / GitHub
- Current company, Current job title
- Years of experience (use standard rounding: 5.7→6, 6.3→6)
- Consent / Terms / Privacy checkboxes — always click to check them
- Cover letter / message from applicant — skip (leave empty, value null)

Rules:
- For file inputs (resume upload): action="upload", value="__RESUME__"
- For custom dropdowns/comboboxes: use click_option
- For LinkedIn's hidden selects: use click_option
- Values MUST come from profile — null if not found
- For yes/no visa/sponsorship questions: applicant is Indian citizen in India → "No"
- For consent/terms checkboxes: ALWAYS check them (action="click", value=null)
- SKIP disabled fields (do NOT include them in the actions list)
- Return ONLY a valid JSON array, no markdown

Profile:
{profile}

Page HTML:
{html}
"""

async def run(page, profile: dict, resume_path: str,
              gemini_client, model: str, on_stuck=None, user_id: int = None) -> tuple[list, list]:
    try:
        # Collect HTML — prioritize frames with form inputs (iframes often hold the form)
        form_html_parts = []
        other_html_parts = []
        for frame in page.frames:
            try:
                fhtml = await frame.inner_html("body")
                if "<input" in fhtml or "<form" in fhtml:
                    form_html_parts.append(fhtml)
                else:
                    other_html_parts.append(fhtml[:1000])  # brief context only
            except Exception:
                pass
        html = ("\n".join(form_html_parts) + "\n" + "\n".join(other_html_parts))[:14000]
        safe = {k: v for k, v in profile.items() if k not in ("password", "_resume_text") and v}

        # Pre-compute full phone number for portals that want a single field
        phone = str(safe.get("phone", "")).strip()
        code  = str(safe.get("phone_country_code", "")).strip()
        if phone and code and not phone.startswith("+"):
            # Strip leading zero from phone if present
            safe["phone_full"] = code + (phone.lstrip("0") if phone.startswith("0") else phone)
        elif phone and phone.startswith("+"):
            safe["phone_full"] = phone
        else:
            safe["phone_full"] = (code + phone) if (code and phone) else phone

        response = gemini_client.models.generate_content(
            model=model,
            contents=PROMPT.format(
                profile=json.dumps(safe, indent=2),
                html=html,
            ),
            config=json_config(),
        )
        actions = parse_gemini_json(response.text or "[]")
        if not isinstance(actions, list):
            return [], []
        logger.info(f"  ContactSkill: {len(actions)} actions")
        return await run_actions(page, actions, resume_path, on_stuck, user_id)
    except Exception as e:
        logger.warning(f"  ContactSkill failed: {e}")
        return [], []
