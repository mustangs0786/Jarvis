"""
apply_skills/account_skill.py
Handles login / register / apply-as-guest flows.
Uses pro model — this is a complex decision with real consequences.

Flow:
1. Check for guest/continue-without-account option first (preferred)
2. If login form — try login, then verify it worked
3. If login failed or no account — switch to register or guest
4. If register — fill registration form
"""

import json
import logging
from .base import parse_gemini_json, run_actions, dispatch_action, json_config
from job_wiki import get_portal_knowledge, save_portal_knowledge

logger = logging.getLogger(__name__)

ANALYZE_PROMPT = """You are analyzing a job application account/login page.

Look carefully at the page and tell me:
1. What options are available (login, register, guest, social login)?
2. Is there a login error message visible (wrong password, account not found)?
3. Is there a "Continue as guest" / "Apply without account" / "Skip" option?

Return JSON:
{{
  "has_login_form": true/false,
  "has_register_form": true/false,
  "has_guest_option": true/false,
  "has_social_login": true/false,
  "login_error_visible": true/false,
  "login_error_text": "error message if any",
  "guest_selector": "CSS selector for guest/continue button if exists, else null",
  "recommended_path": "guest" | "login" | "register",
  "notes": "brief description"
}}

Return ONLY valid JSON, no markdown.

Page HTML:
{html}
"""

FILL_PROMPT = """You are filling a job application {path} form.

Return a JSON array of browser actions to complete the {path} step.

Each action:
- "action": fill | click | click_option | clear_and_fill | press_sequentially | press_key
- "selector": CSS selector
- "value": value from profile (exact) or null
- "label": human-readable field name

Rules:
- Email / "Email Address": use profile email
- "Confirm Email" / "Retype Email" / "Verify Email": use the SAME profile email
- Password / "Choose Password" / "Create Password": use profile password
- "Confirm Password" / "Retype Password" / "Re-enter Password": use the SAME profile password
- ALWAYS provide a value for email and password fields — never null for those (the profile has them)
- For name fields in registration: use profile first_name / last_name
- For phone in registration: use profile phone
- For other fields not in the profile: null
- Return ONLY a valid JSON array, no markdown

IMPORTANT path-specific rules:
- If path="register": Do NOT fill the login form. Click the "Create profile", "Register", "Sign up", or "New user" button/link instead. If a registration form with email/password/name fields is visible, fill those.
- If path="login": Fill email and password fields, then click the Login/Sign in button.

Profile:
{profile}

Page HTML:
{html}
"""


async def run(page, profile: dict, resume_path: str,
              gemini_client, model: str, on_stuck=None, user_id: int = None) -> tuple[list, list]:

    filled_total  = []
    skipped_total = []

    try:
        html_parts = []
        for frame in page.frames:
            try:
                html_parts.append(await frame.inner_html("body"))
            except Exception:
                pass
        html = "\n".join(html_parts)[:8000]
        safe = {k: v for k, v in profile.items() if k not in ("_resume_text",) and v}

        # ── Step 1: Analyze what options are available ────────────────────
        analysis_resp = gemini_client.models.generate_content(
            model=model,
            contents=ANALYZE_PROMPT.format(html=html),
            config=json_config(),
        )
        analysis = parse_gemini_json(analysis_resp.text or "{}")
        logger.info(f"  AccountSkill analysis: {analysis.get('recommended_path')} | "
                    f"guest={analysis.get('has_guest_option')} | "
                    f"error={analysis.get('login_error_visible')} | "
                    f"notes={analysis.get('notes','')[:60]}")

        recommended = analysis.get("recommended_path", "login")
        login_error = analysis.get("login_error_visible", False)

        # ── Step 1.5: Portal memory — account creation comes first ────────
        # If job_wiki says we made an account here, log in and reuse it.
        # Otherwise we've never applied here, so CREATE an account first rather
        # than guessing a login that has no account behind it.
        known = get_portal_knowledge(page.url)
        if known.get("account_created"):
            logger.info("  job_wiki: account exists for this portal → login")
            recommended = "login"
        elif not login_error:
            logger.info("  job_wiki: no account on file → register first")
            recommended = "register"

        # ── Step 2: If login error visible — switch to register/guest ─────
        if login_error:
            logger.info(f"  Login error detected: {analysis.get('login_error_text','')}")
            if analysis.get("has_guest_option"):
                recommended = "guest"
            elif analysis.get("has_register_form"):
                recommended = "register"
            else:
                # Ask user what to do
                if on_stuck:
                    reply = await on_stuck(
                        f"Login failed: {analysis.get('login_error_text', 'unknown error')}\n"
                        "Reply 'register' to create a new account, or 'skip' to stop."
                    )
                    if reply and "register" in reply.lower():
                        recommended = "register"
                    else:
                        return [], ["login_failed"]

        # ── Step 3: Guest path — just click the button ────────────────────
        if recommended == "guest" and analysis.get("has_guest_option"):
            guest_sel = analysis.get("guest_selector")
            if guest_sel:
                try:
                    el = page.locator(guest_sel).first
                    if await el.count() > 0 and await el.is_visible():
                        await el.click()
                        logger.info(f"  Guest path: clicked {guest_sel}")
                        await page.wait_for_timeout(1500)
                        return ["Guest continue"], []
                except Exception as e:
                    logger.warning(f"  Guest click failed: {e}")
            # Fallback — search for guest button by text
            for txt in ["Continue as guest", "Apply as guest", "Skip sign in",
                        "Continue without account", "Apply without account", "Guest"]:
                try:
                    btn = page.locator(f"button:has-text('{txt}'), a:has-text('{txt}')").first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                        logger.info(f"  Guest path: clicked '{txt}'")
                        await page.wait_for_timeout(1500)
                        return ["Guest continue"], []
                except Exception:
                    continue

        # ── Step 4: Login or Register — ask Gemini to fill the form ───────
        path = "register" if recommended == "register" else "login"
        fill_resp = gemini_client.models.generate_content(
            model=model,
            contents=FILL_PROMPT.format(
                path=path,
                profile=json.dumps(safe, indent=2),
                html=html,
            ),
            config=json_config(),
        )
        actions = parse_gemini_json(fill_resp.text or "[]")
        if not isinstance(actions, list):
            return [], []

        logger.info(f"  AccountSkill {path}: {len(actions)} actions")
        filled, skipped = await run_actions(page, actions, resume_path, on_stuck, user_id)
        filled_total  += filled
        skipped_total += skipped

        # ── Step 5: Wait and check if login/register succeeded ────────────
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

        # Re-check for login error after attempting
        new_parts = []
        for frame in page.frames:
            try:
                new_parts.append(await frame.inner_html("body"))
            except Exception:
                pass
        new_html = "\n".join(new_parts)[:4000]
        check_resp = gemini_client.models.generate_content(
            model=model,
            contents=ANALYZE_PROMPT.format(html=new_html),
            config=json_config(),
        )
        new_analysis = parse_gemini_json(check_resp.text or "{}")
        if new_analysis.get("login_error_visible"):
            logger.warning(f"  Still on login page after attempt: {new_analysis.get('login_error_text','')}")
            # If we tried login and failed, switch to register
            if path == "login" and new_analysis.get("has_register_form"):
                logger.info("  Switching to register flow")
                path = "register"
                reg_resp = gemini_client.models.generate_content(
                    model=model,
                    contents=FILL_PROMPT.format(
                        path="register",
                        profile=json.dumps(safe, indent=2),
                        html=new_html,
                    ),
                    config=json_config(),
                )
                reg_actions = parse_gemini_json(reg_resp.text or "[]")
                if isinstance(reg_actions, list):
                    f2, s2 = await run_actions(page, reg_actions, resume_path, on_stuck, user_id)
                    filled_total  += f2
                    skipped_total += s2

        # ── Step 6: Remember this portal so next time we just log in ──────
        if not new_analysis.get("login_error_visible") and (filled_total or path == "register"):
            save_portal_knowledge(page.url, {
                "account_created": True,
                "email":           profile.get("email", ""),
                "path_used":       path,
                "portal_notes":    analysis.get("notes", ""),
            })
            logger.info(f"  job_wiki: saved account knowledge ({path}) for this portal")

    except Exception as e:
        logger.warning(f"  AccountSkill failed: {e}")

    return filled_total, skipped_total
