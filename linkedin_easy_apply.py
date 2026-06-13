"""
linkedin_easy_apply.py
======================
Automates LinkedIn Easy Apply forms using saved session cookies.

Flow:
    1. Load cookies → open job page
    2. Click "Easy Apply" button → modal opens
    3. For each step: find fields, fill from profile, ask user if unknown
    4. Click Next/Review/Submit
    5. Return result

Usage (CLI test):
    python linkedin_easy_apply.py <linkedin_job_url> <resume_pdf>
"""

import os
import asyncio
import logging
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
load_dotenv()

from profile_manager import load_profile, learn_answer, get_field_value
from linkedin_url_extractor import load_cookies, HEADERS

import json
import re

logger = logging.getLogger(__name__)

MAX_STEPS = 15

SKIP_LABELS = {
    "follow", "follow company", "receive updates", "newsletter",
    "i agree", "i certify", "terms", "privacy",
    "cover letter", "upload cover letter", "add a cover letter",
}


def _q(text) -> str:
    """Quote text for CSS attribute / :has-text() selectors — picks a quote
    char not present in the text so values like "Bachelor's degree" don't
    break the selector."""
    text = str(text)
    if '"' in text and "'" in text:
        text = text.replace('"', "")
    return f"'{text}'" if '"' in text else f'"{text}"'


def _azure_json(prompt: str, image_b64: str = None):
    """Azure GPT call (APPLY_MODEL, e.g. gpt-5.4-mini) via apply_llm's client,
    configured through AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_KEY."""
    from apply_llm import _openai_json
    return _openai_json(prompt, image_b64)


def _gemini_json(prompt: str, image_b64: str, gemini_client, model: str):
    """Gemini call returning parsed JSON (text + optional screenshot)."""
    parts = [{"text": prompt}]
    if image_b64:
        parts.append({"inline_data": {"mime_type": "image/png", "data": image_b64}})
    resp = gemini_client.models.generate_content(
        model=model, contents=[{"parts": parts}])
    raw = (resp.text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def _llm_json(prompt: str, image_b64: str = None,
              gemini_client=None, model: str = ""):
    """Engine LLM call honoring APPLY_LLM from .env:
        openai → Azure GPT first, Gemini as backup
        gemini → Gemini first, Azure GPT as backup
    Either side failing (429 quota, timeout, bad JSON) falls through to the
    other, so one dead provider never stalls an apply run."""
    from apply_llm import APPLY_LLM
    if APPLY_LLM == "openai":
        try:
            return _azure_json(prompt, image_b64)
        except Exception as e:
            logger.warning(f"  Azure GPT failed ({e}) — trying Gemini")
            if not gemini_client:
                raise
            return _gemini_json(prompt, image_b64, gemini_client, model)
    try:
        if not gemini_client:
            raise RuntimeError("no gemini client")
        return _gemini_json(prompt, image_b64, gemini_client, model)
    except Exception as e:
        logger.warning(f"  Gemini failed ({e}) — trying Azure GPT")
        return _azure_json(prompt, image_b64)


# ── Session whiteboard ────────────────────────────────────────────────────────

class Whiteboard:
    """Tracks every field seen during apply — filled, skipped, or pending."""

    def __init__(self):
        self.steps: list[dict] = []  # one entry per modal step

    def start_step(self, step_num: int, fields: list):
        self.steps.append({
            "step":    step_num,
            "total":   len(fields),
            "filled":  [],
            "skipped": [],
            "pending": [f.get("label", "") for f in fields],
        })

    def mark_filled(self, label: str):
        if self.steps:
            s = self.steps[-1]
            s["filled"].append(label)
            if label in s["pending"]:
                s["pending"].remove(label)

    def mark_skipped(self, label: str):
        if self.steps:
            s = self.steps[-1]
            s["skipped"].append(label)
            if label in s["pending"]:
                s["pending"].remove(label)

    def print_step_summary(self):
        if not self.steps:
            return
        s = self.steps[-1]
        lines = [
            f"\n  ┌── Step {s['step']} Whiteboard ({s['total']} fields) ──────────",
            f"  │  ✅ Filled  : {', '.join(s['filled']) or 'none'}",
            f"  │  ❌ Skipped : {', '.join(s['skipped']) or 'none'}",
            f"  │  ⏳ Pending : {', '.join(s['pending']) or 'none'}",
            f"  └────────────────────────────────────────────────────",
        ]
        for line in lines:
            logger.info(line)

    def print_final_summary(self):
        all_filled  = [f for s in self.steps for f in s["filled"]]
        all_skipped = [f for s in self.steps for f in s["skipped"]]
        all_pending = [f for s in self.steps for f in s["pending"]]
        lines = [
            "\n  ╔══ FINAL WHITEBOARD SUMMARY ══════════════════════════",
            f"  ║  Steps completed : {len(self.steps)}",
            f"  ║  ✅ Total filled  : {len(all_filled)}",
            f"  ║     {', '.join(all_filled) or 'none'}",
            f"  ║  ❌ Total skipped : {len(all_skipped)}",
            f"  ║     {', '.join(all_skipped) or 'none'}",
            f"  ║  ⏳ Still pending : {len(all_pending)}",
            f"  ║     {', '.join(all_pending) or 'none'}",
            "  ╚══════════════════════════════════════════════════════",
        ]
        for line in lines:
            logger.info(line)


# ── Result ────────────────────────────────────────────────────────────────────

class EasyApplyResult:
    def __init__(self):
        self.status          = "pending"
        self.portal          = "linkedin"
        self.job_title       = ""   # app.py reads these when reporting the result
        self.company         = ""
        self.fields_filled   = []
        self.fields_skipped  = []
        self.fields_learned  = []
        self.screenshot_path = ""
        self.error           = ""
        self.steps_completed = 0


# ── Resume text extractor ─────────────────────────────────────────────────────

def extract_resume_text(resume_path: str) -> str:
    """Extract plain text from a PDF resume."""
    try:
        import fitz  # PyMuPDF
        doc  = fitz.open(resume_path)
        text = "\n".join(p.get_text() for p in doc)
        doc.close()
        return text.strip()
    except Exception as e:
        logger.warning(f"  Resume text extraction failed: {e}")
        return ""


# ── Gemini modal analyzer — returns executable actions ────────────────────────

GEMINI_PROMPT = """You are controlling a browser to fill a LinkedIn Easy Apply form step.

You will receive:
1. The full HTML of the current modal step
2. The applicant's profile JSON (source of truth for all values)
3. The applicant's resume text (fallback if profile missing a value)

Your job: return a JSON array of browser actions to fully fill this form step.

Each action must have:
- "action": one of: fill, click, click_option, upload, press_sequentially, press_key, scroll_into_view, hover, clear_and_fill, wait
- "selector": CSS selector to find the element (prefer #id > [name='x'] > [aria-label='x'] > visible text selector)
- "value": value to use (string), or null if not needed
- "label": human-readable name of this field (for logging)

Action meanings:
- fill            → el.fill(value)  — for text/email/tel/number inputs
- click           → el.click()      — for buttons, labels, radio options, checkboxes, combobox triggers
- click_option    → open a dropdown/combobox then click the matching option — use for LinkedIn's custom select components; set value = option text to select
- upload          → set_input_files(value) — for file inputs; set value = "__RESUME__" to use the resume PDF
- press_sequentially → type character by character — for autocomplete inputs that need keystroke events
- press_key       → el.press(value) — e.g. value="Enter", "Tab", "Escape"
- scroll_into_view → scroll element into view before interacting
- hover           → hover over element (to reveal hidden fields)
- clear_and_fill  → clear the field then fill — for inputs that resist direct fill
- wait            → wait N milliseconds; set value = "500" etc.

Rules:
- Read the HTML carefully — identify EVERY fillable field in this step
- For LinkedIn radio groups: the inputs are visually hidden; you must CLICK the <label> element, not the <input>
- For LinkedIn custom dropdowns (role=combobox, role=listbox): use click_option, NOT fill
- For standard <select> tags: use click_option with the option text as value
- For date fields: use fill with format matching the input placeholder
- For numeric steppers (+/- buttons): use click on the correct button
- If you see ANY file input (input[type='file']) in the HTML: you MUST include an upload action for it with value "__RESUME__". Never skip it.
- Skip: follow company checkboxes, I agree checkboxes, privacy/terms, submit/next buttons, hidden inputs
- Values MUST come from profile JSON or resume text — NEVER invent values unrelated to the candidate.
- For numeric "years of experience in X" questions: if X appears in the resume, estimate years from context. If X is broadly in the candidate's domain, use years_experience from profile as an upper-bound estimate. Never return null for these — always provide a whole number ≥ 1.
- For "how soon can you join" / "notice period in days" fields: convert notice_period from profile to days (e.g. "30 days"→30, "2 months"→60, "immediate"→0, "serving notice"→30). If notice_period is missing, default to 30. Never return null.
- For yes/no skill questions: answer "Yes" if the resume clearly mentions it, "No" otherwise. Never return null.
- For yes/no questions about visa/sponsorship/work authorization: applicant is Indian citizen in India, use "No" or equivalent.
- For numeric experience fields that require a whole number: use standard rounding (e.g. 5.7 → 6, 6.3 → 6, 6.5 → 7). Never truncate.
- For fields completely unrelated to the candidate with no basis at all in profile or resume: return null.
- Return ONLY a valid JSON array, no markdown, no explanation

=== PROFILE JSON ===
{profile}

=== RESUME TEXT ===
{resume_text}

=== FORM HTML ===
{modal_html}
"""


def _compact_html(html: str, max_chars: int = 60_000) -> str:
    """Shrink raw modal HTML before sending it to the LLM: drop svg/style/script
    blocks and HTML comments, collapse whitespace, cap total size. LinkedIn
    modals carry hundreds of KB of decoration — huge prompts made the analyze
    call take minutes (or stall) without improving field detection."""
    html = re.sub(r"<(svg|style|script)\b.*?</\1>", "", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n", html)
    return html[:max_chars]


async def analyze_modal(modal_html: str, profile: dict, resume_text: str,
                        gemini_client, model: str) -> list:
    """Send compacted modal HTML + profile to the apply LLM (APPLY_LLM order).
    Returns list of browser actions."""
    if not gemini_client and not os.getenv("AZURE_OPENAI_KEY"):
        return []

    safe_profile = {k: v for k, v in profile.items()
                    if k not in ("password",) and v}

    prompt = GEMINI_PROMPT.format(
        profile=json.dumps(safe_profile, indent=2),
        resume_text=resume_text[:5000],
        modal_html=_compact_html(modal_html),
    )

    try:
        data = _llm_json(
            prompt + '\n\nIf you must return a JSON object, wrap the array as {"actions": [...]}.',
            gemini_client=gemini_client, model=model,
        )
        if isinstance(data, dict):
            data = data.get("actions") or next(
                (v for v in data.values() if isinstance(v, list)), [])
        if isinstance(data, list):
            logger.info(f"  Planned {len(data)} actions")
            return data
    except Exception as e:
        logger.error(f"  Form analysis failed on both LLMs: {e}")
    return []


# ── Modal helpers ─────────────────────────────────────────────────────────────

async def is_modal_open(page) -> bool:
    for sel in [
        "[data-test-modal]",
        ".jobs-easy-apply-modal",
        ".artdeco-modal",
        "[role='dialog']",
    ]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                return True
        except Exception:
            continue
    # NOTE: no body-text fallback here — phrases like "easy apply" appear on
    # every LinkedIn job page (the button itself), which made this function
    # return True even when no modal was open.
    return False


async def click_easy_apply(page) -> bool:
    """Click the Easy Apply button. Returns True if modal opened."""
    apply_el = None
    APPLY_SELS = [
        "button:has-text('Easy Apply')",
        "a:has-text('Easy Apply')",
        "button:has-text('Apply')",
        "a:has-text('Apply')",
    ]

    # Pass 1: search within main job pane scopes to avoid sidebar job cards
    for scope_sel in [".jobs-unified-top-card", ".job-view-layout", ".jobs-details", "main"]:
        scope = page.locator(scope_sel).first
        if not await scope.count():
            continue
        for sel in APPLY_SELS:
            try:
                el = scope.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    txt = (await el.inner_text()).strip()
                    if len(txt) < 60:
                        apply_el = el
                        logger.info(f"  Found in {scope_sel}: '{txt}'")
                        break
            except Exception:
                continue
        if apply_el:
            break

    # Pass 2: page-wide fallback — pick first visible element with short text
    if not apply_el:
        for sel in APPLY_SELS:
            try:
                els = page.locator(sel)
                for i in range(min(await els.count(), 15)):
                    el = els.nth(i)
                    if not await el.is_visible():
                        continue
                    txt = (await el.inner_text()).strip()
                    if len(txt) < 60:
                        apply_el = el
                        logger.info(f"  Fallback: '{txt}'")
                        break
            except Exception:
                continue
            if apply_el:
                break

    if not apply_el:
        logger.info("  No Apply button found")
        return False

    btn_text = (await apply_el.inner_text()).strip().lower()
    logger.info(f"  Button text: '{btn_text}'")

    # Click regardless — url_extractor already confirmed Easy Apply before routing here.
    # Button may say 'apply' instead of 'easy apply' depending on UI state.
    # Listen for new tab (external apply) before clicking
    external_url = None
    try:
        async with page.context.expect_page(timeout=4000) as new_page_info:
            await apply_el.click()
            logger.info("  Clicked Apply button")
        new_page = await new_page_info.value
        await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
        external_url = new_page.url
        logger.info(f"  External apply: new tab opened → {external_url[:80]}")
        # Store external URL on the page object so run_easy_apply can retrieve it
        page._external_apply_url = external_url
        await new_page.close()
        return False  # not Easy Apply
    except Exception:
        # No new tab opened — the click may have opened a modal
        logger.info("  Clicked Apply button (no new tab)")

    # Wait for page to settle (handles both modal and navigation cases)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass

    # Wait up to 10 seconds for modal to appear
    for i in range(10):
        await page.wait_for_timeout(1000)
        if await is_modal_open(page):
            logger.info(f"  Modal opened (after {i+1}s)")
            return True
        logger.info(f"  Waiting for modal... ({i+1}s)")

    # Last check — take screenshot to debug
    logger.info("  Modal did not open after 10s")
    return False


async def find_next_button(page):
    """Find the Next/Review/Submit button WITHOUT clicking it.
    Returns (element, kind) where kind is 'submit'|'review'|'next'|'none'.
    Scoped to the modal so a stray 'Next' elsewhere on the page never matches.
    (:has-text() is case-insensitive, so one spelling per button suffices.)"""
    scope = page.locator(MODAL_SEL).first
    try:
        if not await scope.count():
            scope = page
    except Exception:
        scope = page
    for text, kind in [
        ("Submit application", "submit"),
        ("Review", "review"),
        ("Next", "next"),
    ]:
        try:
            btn = scope.locator(f"button:has-text('{text}')").first
            if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                return btn, kind
        except Exception:
            continue
    return None, "none"


async def check_submitted(page) -> bool:
    """Verify the application was actually submitted.
    Looks for the post-submit dialog text or the job card's 'Applied' badge —
    never a bare 'applied' substring, which false-positives on job titles
    like 'Applied Scientist' or companies like 'Applied Materials'."""
    try:
        body = (await page.inner_text("body") or "").lower()
        if any(p in body for p in [
            "application was sent", "application submitted",
            "your application was sent", "you've applied",
            "successfully applied",
        ]):
            return True
    except Exception:
        pass
    try:
        badge = page.locator(
            ".artdeco-inline-feedback__message, .jobs-s-apply__applied-date, "
            ".post-apply-timeline"
        ).first
        if await badge.count() > 0 and await badge.is_visible():
            if "applied" in ((await badge.inner_text()) or "").lower():
                return True
    except Exception:
        pass
    return False


# ── Label extractor ───────────────────────────────────────────────────────────

async def get_label(modal, input_el) -> str:
    """Get the label text for an input element."""
    try:
        aria = await input_el.get_attribute("aria-label") or ""
        if aria.strip():
            return aria.strip()

        input_id = await input_el.get_attribute("id") or ""
        if input_id:
            lbl = modal.locator(f"label[for='{input_id}']").first
            if await lbl.count() > 0:
                text = (await lbl.inner_text() or "").strip()
                if text:
                    return text

        placeholder = await input_el.get_attribute("placeholder") or ""
        if placeholder.strip():
            return placeholder.strip()

        name = await input_el.get_attribute("name") or ""
        if name.strip():
            return name.replace("-", " ").replace("_", " ").strip()

    except Exception:
        pass
    return ""


# ── Step filler ───────────────────────────────────────────────────────────────

MODAL_SEL = "[data-test-modal], .jobs-easy-apply-modal, .artdeco-modal, [role='dialog']"



async def handle_autocomplete(page, modal, label: str, value: str,
                              gemini_client, model: str,
                              max_attempts: int = 2) -> bool:
    """
    After typing into a field, take a screenshot + read HTML,
    pass both to Pro Gemini to detect and click any dropdown.
    Retries up to max_attempts times if dropdown still visible.
    """
    if not gemini_client and not os.getenv("AZURE_OPENAI_KEY"):
        return False

    for attempt in range(max_attempts):
        try:
            # Scroll input into view so dropdown is visible in screenshot
            try:
                el = page.locator(f"[aria-label*={_q(label)}], input:visible").first
                await el.scroll_into_view_if_needed()
            except Exception:
                pass

            # Full page screenshot — captures dropdown even if below modal
            ss_path = f"output/autocomplete_{label[:15].replace(' ','_')}_{attempt}.png"
            await page.screenshot(path=ss_path, full_page=False)  # viewport only, not full scroll

            # Read screenshot as bytes for Gemini vision
            import base64
            with open(ss_path, "rb") as f:
                img_bytes = base64.b64encode(f.read()).decode()

            # Use full body HTML — dropdown is often outside the modal DOM
            try:
                body_html = (await page.inner_html("body"))[:4000]
            except Exception:
                body_html = (await modal.inner_html())[:4000]

            prompt = f"""You are controlling a browser to fill a job application form.

The field "{label}" was just filled with "{value}".
Look at the screenshot (viewport) and HTML — is there an autocomplete/suggestion dropdown visible?

The dropdown may appear BELOW the input field, outside the modal.
Look at the screenshot carefully — the dropdown is often a list of clickable city/location/company names.

If YES: return the CSS selector of the best matching option to click.
If NO: return has_dropdown as false.

HTML (body):
{body_html}

Reply as JSON only: {{"has_dropdown": true/false, "selector": "selector_or_null", "option_text": "text"}}"""

            data = _llm_json(prompt, img_bytes, gemini_client, model)

            if not data.get("has_dropdown"):
                logger.info(f"  Autocomplete: no dropdown detected (attempt {attempt+1})")
                return False

            sel = data.get("selector")
            if not sel:
                break

            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                logger.info(f"  Autocomplete: clicked '{data.get('option_text', '')}' (attempt {attempt+1})")
                await page.wait_for_timeout(600)
                return True
            else:
                logger.info(f"  Autocomplete: selector not found '{sel}' — retrying")

        except Exception as e:
            logger.debug(f"  Autocomplete attempt {attempt+1} failed: {e}")

    return False


async def _maybe_autocomplete(page, modal, el, label: str, value: str,
                              gemini_client, model: str):
    """Run the (expensive) vision autocomplete check only for real typeahead
    inputs — plain text fields never open dropdowns, and a screenshot+LLM call
    after every fill was the engine's biggest time sink."""
    try:
        ac   = (await el.get_attribute("aria-autocomplete") or "").lower()
        role = (await el.get_attribute("role") or "").lower()
    except Exception:
        ac = role = ""
    if ac in ("list", "both") or role == "combobox":
        await handle_autocomplete(page, modal, label, value, gemini_client, model)


async def _dispatch_action(page, modal, action: dict, resume_path: str,
                           user_id: int, result: EasyApplyResult,
                           gemini_client, model: str,
                           whiteboard: Whiteboard,
                           on_stuck: Callable, on_screenshot: Callable,
                           filled_selectors: set,
                           profile: dict = None):
    """
    Execute a single Gemini-planned browser action.
    Handles all 10 action types — no type-specific branching outside this function.
    """
    act      = action.get("action", "").strip().lower()
    selector = action.get("selector", "").strip()
    value    = action.get("value")   # may be None
    label    = action.get("label", selector or act)

    if not act:
        return
    if selector in filled_selectors:
        logger.debug(f"  Already filled: {selector}")
        return

    # ── wait — no element needed ──────────────────────────────────────────
    if act == "wait":
        try:
            ms = int(re.sub(r"[^0-9]", "", str(value or "")) or 500)
        except Exception:
            ms = 500
        await page.wait_for_timeout(min(ms, 10000))
        logger.info(f"  Wait {ms}ms")
        return

    # ── Resolve element (with modal fallback) ────────────────────────────
    el = None
    if selector:
        try:
            candidate = page.locator(selector).first
            if await candidate.count() > 0:
                el = candidate
        except Exception:
            pass
        if el is None:
            try:
                candidate = modal.locator(selector).first
                if await candidate.count() > 0:
                    el = candidate
            except Exception:
                pass

    if el is None and act not in ("wait",):
        logger.debug(f"  Element not found: {selector} [{label}]")
        result.fields_skipped.append(label)
        if whiteboard: whiteboard.mark_skipped(label)
        return

    # ── If value is null/None and we need one — ask user ─────────────────
    needs_value = act in ("fill", "click_option", "upload", "press_sequentially", "clear_and_fill")
    # 0 is a legitimate answer (e.g. notice period 0 days) — only None/"" are missing
    if needs_value and (value is None or str(value).strip() == ""):
        # Try profile lookup as last resort
        value = get_field_value(label, profile or {}) or None
        if not value and on_stuck:
            if on_screenshot:
                try:
                    ss = f"output/ea_{user_id}_ask_{label[:20].replace(' ','_')}.png"
                    await modal.screenshot(path=ss)
                    await on_screenshot(ss)
                except Exception:
                    pass
            value = await on_stuck(label)
            if value and value.lower() not in ("skip", ""):
                learn_answer(user_id, label, value)
                result.fields_learned.append(label)
        if not value or (isinstance(value, str) and value.lower() == "skip"):
            result.fields_skipped.append(label)
            if whiteboard: whiteboard.mark_skipped(label)
            return

    try:
        # ── scroll_into_view ──────────────────────────────────────────────
        if act == "scroll_into_view":
            await el.scroll_into_view_if_needed()
            logger.info(f"  Scrolled into view: {label}")
            return

        # ── hover ─────────────────────────────────────────────────────────
        elif act == "hover":
            await el.hover()
            logger.info(f"  Hovered: {label}")
            return

        # ── press_key ─────────────────────────────────────────────────────
        elif act == "press_key":
            await el.press(str(value))
            logger.info(f"  Key press: {label} → {value}")
            return

        # ── upload ────────────────────────────────────────────────────────
        elif act == "upload":
            path = resume_path  # always use the resume passed in, ignore Gemini's value
            if path and Path(path).exists():
                await el.set_input_files(path)
                logger.info(f"  Uploaded: {Path(path).name} [{label}]")
                result.fields_filled.append(label)
                if whiteboard: whiteboard.mark_filled(label)
                filled_selectors.add(selector)
                await page.wait_for_timeout(800)
            else:
                logger.warning(f"  Upload path not found: {path}")
                result.fields_skipped.append(label)
                if whiteboard: whiteboard.mark_skipped(label)

        # ── fill ──────────────────────────────────────────────────────────
        elif act == "fill":
            await el.scroll_into_view_if_needed()
            await el.fill(str(value))
            logger.info(f"  Filled: {label} = {str(value)[:50]}")
            await page.wait_for_timeout(600)
            await _maybe_autocomplete(page, modal, el, label, str(value), gemini_client, model)
            result.fields_filled.append(label)
            if whiteboard: whiteboard.mark_filled(label)
            filled_selectors.add(selector)

        # ── clear_and_fill ────────────────────────────────────────────────
        elif act == "clear_and_fill":
            await el.scroll_into_view_if_needed()
            await el.click(click_count=3)
            await el.press("Control+a")
            await el.press("Backspace")
            await el.fill(str(value))
            logger.info(f"  Clear+Fill: {label} = {str(value)[:50]}")
            await page.wait_for_timeout(600)
            await _maybe_autocomplete(page, modal, el, label, str(value), gemini_client, model)
            result.fields_filled.append(label)
            if whiteboard: whiteboard.mark_filled(label)
            filled_selectors.add(selector)

        # ── press_sequentially ────────────────────────────────────────────
        elif act == "press_sequentially":
            await el.scroll_into_view_if_needed()
            await el.click(click_count=3)
            await el.press_sequentially(str(value), delay=60)
            logger.info(f"  Typed sequentially: {label} = {str(value)[:50]}")
            await page.wait_for_timeout(800)
            await _maybe_autocomplete(page, modal, el, label, str(value), gemini_client, model)
            result.fields_filled.append(label)
            if whiteboard: whiteboard.mark_filled(label)
            filled_selectors.add(selector)

        # ── click ─────────────────────────────────────────────────────────
        elif act == "click":
            await el.scroll_into_view_if_needed()
            await el.click()
            logger.info(f"  Clicked: {label}")
            await page.wait_for_timeout(300)
            result.fields_filled.append(label)
            if whiteboard: whiteboard.mark_filled(label)
            filled_selectors.add(selector)

        # ── click_option — for custom dropdowns/comboboxes/select ────────
        elif act == "click_option":
            await el.scroll_into_view_if_needed()

            # First try native <select> — works instantly if it's a real select
            tag = (await el.evaluate("e => e.tagName")).lower()
            if tag == "select":
                options = [o.strip() for o in await el.locator("option").all_inner_texts() if o.strip()]
                best = next(
                    (o for o in options if str(value).lower() in o.lower() or o.lower() in str(value).lower()),
                    None
                )
                if best:
                    await el.select_option(label=best, timeout=5000)
                    logger.info(f"  Native select: {label} = {best}")
                    result.fields_filled.append(label)
                    if whiteboard: whiteboard.mark_filled(label)
                    filled_selectors.add(selector)
                    return

            # Custom combobox — click to open, then click matching option
            await el.click()
            await page.wait_for_timeout(600)

            # Try to find the dropdown list (LinkedIn renders outside modal)
            option_found = False
            for list_sel in [
                f"[role='option']:has-text({_q(value)})",
                f"li:has-text({_q(value)})",
                f"[role='listbox'] [role='option']",
                f"ul[role='listbox'] li",
                f".dropdown__option",
                f"[data-test-autocomplete-result]",
            ]:
                try:
                    opts = page.locator(list_sel)
                    count = await opts.count()
                    if count == 0:
                        continue
                    # Find best matching option
                    for i in range(min(count, 20)):
                        opt_text = (await opts.nth(i).inner_text() or "").strip()
                        if str(value).lower() in opt_text.lower() or opt_text.lower() in str(value).lower():
                            await opts.nth(i).click()
                            logger.info(f"  click_option: {label} = {opt_text}")
                            result.fields_filled.append(label)
                            if whiteboard: whiteboard.mark_filled(label)
                            filled_selectors.add(selector)
                            option_found = True
                            await page.wait_for_timeout(400)
                            break
                    if option_found:
                        break
                except Exception:
                    continue

            if not option_found:
                logger.warning(f"  click_option: no match found for '{value}' [{label}]")
                result.fields_skipped.append(label)
                if whiteboard: whiteboard.mark_skipped(label)

        else:
            logger.warning(f"  Unknown action '{act}' for [{label}]")

    except Exception as e:
        logger.info(f"  ⚠ Action error [{act}|{label}]: {e}")
        result.fields_skipped.append(label)
        if whiteboard: whiteboard.mark_skipped(label)


async def fill_step(page, profile: dict, resume_path: str,
                    user_id: int, on_stuck: Callable, result: EasyApplyResult,
                    gemini_client=None, model: str = "", pro_model: str = "",
                    resume_text: str = "", whiteboard: Whiteboard = None,
                    on_screenshot: Callable = None):
    """
    Fill all fields in the current modal step.
    Gemini reads the full modal HTML + profile and returns a list of browser actions.
    A thin dispatcher executes each action — no field-type branching here.
    """
    try:
        modal = page.locator(MODAL_SEL).first
        if not await modal.count():
            return
        modal_html = await modal.inner_html()
    except Exception:
        return

    if not gemini_client:
        return

    logger.info(f"  Modal HTML: {len(modal_html)} chars — asking Gemini for actions...")
    actions = await analyze_modal(modal_html, profile, resume_text, gemini_client, model)
    if not actions:
        return

    if whiteboard:
        # Use labels from actions for whiteboard tracking
        field_labels = [a.get("label", "") for a in actions if a.get("label")]
        whiteboard.start_step(len(whiteboard.steps) + 1,
                              [{"label": l} for l in field_labels])

    filled_selectors: set = set()

    for action in actions:
        await _dispatch_action(
            page=page, modal=modal, action=action,
            resume_path=resume_path, user_id=user_id,
            result=result, gemini_client=gemini_client,
            model=pro_model or model,
            whiteboard=whiteboard,
            on_stuck=on_stuck, on_screenshot=on_screenshot,
            filled_selectors=filled_selectors,
            profile=profile,
        )
        await page.wait_for_timeout(200)

    if whiteboard:
        whiteboard.print_step_summary()


# ── Main entry ────────────────────────────────────────────────────────────────

async def autonomous_nav_recovery(page, step_num: int, gemini_client,
                                   model: str, on_screenshot, user_id: int) -> bool:
    """
    When Next/Submit button not found:
    1. Take screenshot
    2. Ask Pro Gemini what's blocking navigation (validation error, dropdown, missing field)
    3. LLM returns action to fix it
    4. Playwright executes fix
    """
    if not gemini_client and not os.getenv("AZURE_OPENAI_KEY"):
        return False

    try:
        import base64
        ss_path = f"output/ea_{user_id}_recovery{step_num}.png"
        await page.screenshot(path=ss_path, full_page=False)

        with open(ss_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        body_html = (await page.inner_html("body"))[:5000]

        prompt = f"""You are controlling a browser filling a LinkedIn Easy Apply form.
Step {step_num} is complete but the Next/Submit button cannot be found or clicked.

Look at the screenshot and HTML. Diagnose what is blocking navigation:
- Validation error on a field (red border, error message)?
- A dropdown/autocomplete still open?
- A required field not filled?
- A checkbox that needs to be checked?
- Something else?

Then return ONE action to fix it:

Reply as JSON only:
{{
  "issue": "short description of what's blocking",
  "action": "click|fill|select|check|dismiss",
  "selector": "css selector of element to interact with",
  "value": "value to fill if action is fill, else null"
}}

HTML:
{body_html}"""

        data = _llm_json(prompt, img_b64, gemini_client, model)

        logger.info(f"  Recovery diagnosis: {data.get('issue', '?')}")
        action = data.get("action", "")
        selector = data.get("selector", "")
        value = data.get("value", "")

        if not selector:
            return False

        el = page.locator(selector).first
        if not await el.count():
            return False

        if action == "click":
            await el.click()
            logger.info(f"  Recovery: clicked {selector}")
        elif action == "fill" and value:
            await el.fill(str(value))
            logger.info(f"  Recovery: filled {selector} = {value}")
        elif action == "select" and value:
            await el.select_option(label=value, timeout=3000)
            logger.info(f"  Recovery: selected {selector} = {value}")
        elif action == "check":
            if not await el.is_checked():
                await el.click()
            logger.info(f"  Recovery: checked {selector}")
        elif action == "dismiss":
            await page.keyboard.press("Escape")
            logger.info("  Recovery: dismissed with Escape")

        await page.wait_for_timeout(1000)
        return True

    except Exception as e:
        logger.debug(f"  Recovery failed: {e}")
        return False


async def run_easy_apply(
    job_url:       str,
    resume_path:   str,
    user_id:       int,
    gemini_client  = None,
    model:         str = "",
    pro_model:     str = "",
    on_stuck:      Callable = None,
    on_screenshot: Callable = None,
    on_notify:     Callable = None,
    autopilot:     bool = True,
) -> EasyApplyResult:
    from playwright.async_api import async_playwright

    profile     = load_profile(user_id)
    result      = EasyApplyResult()
    cookies     = load_cookies()
    wb          = Whiteboard()
    # Use resume text already stored in profile; extract from PDF as fallback
    resume_text = profile.get("_resume_text", "") or extract_resume_text(resume_path)
    Path("output").mkdir(exist_ok=True)

    if not cookies:
        result.status = "failed"
        result.error  = "No LinkedIn session. Run: python linkedin_url_extractor.py login"
        return result

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=HEADERS["User-Agent"],
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        cast = None  # live screencast task — started after first navigation

        try:
            logger.info(f"  Loading: {job_url}")
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            from screencast import start_screencast
            cast = start_screencast(page, on_screenshot, user_id)

            # Debug: screenshot + title so we can see what loaded
            page_title = await page.title()
            logger.info(f"  Page title: {page_title}")
            debug_ss = f"output/ea_{user_id}_pageload.png"
            await page.screenshot(path=debug_ss, full_page=False)
            logger.info(f"  Page screenshot → {debug_ss}")

            if on_notify:
                await on_notify("📋 Job page loaded — clicking Easy Apply...")

            opened = await click_easy_apply(page)
            if not opened:
                # Check if we captured an external apply URL
                external_url = getattr(page, "_external_apply_url", None)
                if external_url:
                    logger.info(f"  Routing to external apply: {external_url[:80]}")
                    from screencast import stop_screencast as _stop
                    await _stop(cast)
                    cast = None
                    await browser.close()
                    # Not Easy Apply after all — clicking Apply opened the company's
                    # own portal in a new tab. Drive it with the SAME mature engine the
                    # web app / bot use for direct external + Workday URLs
                    # (apply_orchestrator.run_application = converge_page + workday.py +
                    # vision), NOT the legacy skill-router. Imported lazily to avoid a
                    # circular import (apply_orchestrator imports run_easy_apply).
                    from apply_orchestrator import run_application
                    ext_result = await run_application(
                        job_url=external_url,
                        resume_path=resume_path,
                        user_id=user_id,
                        gemini_client=gemini_client,
                        model=model,
                        pro_model=pro_model,
                        on_stuck=on_stuck,
                        on_screenshot=on_screenshot,
                        on_notify=on_notify,
                        auto_submit=True,
                    )
                    # run_application returns an ExternalApplyResult — same shape;
                    # copy its fields onto our EasyApplyResult.
                    result.status          = ext_result.status
                    result.fields_filled   = ext_result.fields_filled
                    result.fields_skipped  = ext_result.fields_skipped
                    result.steps_completed = ext_result.steps_completed
                    result.error           = ext_result.error
                    result.screenshot_path = ext_result.screenshot_path
                    return result

                result.status = "failed"
                result.error  = "Could not open Easy Apply modal"
                ss = f"output/ea_{user_id}_no_modal.png"
                await page.screenshot(path=ss, full_page=True)
                result.screenshot_path = ss
                if on_screenshot:
                    await on_screenshot(ss)
                await browser.close()
                return result

            if on_notify:
                await on_notify("✅ Easy Apply modal opened — filling your details...")

            async def confirm_submit() -> bool:
                """Ask the user BEFORE clicking Submit — it can't be undone.
                Autopilot submits without asking; questions are reserved for
                genuinely stuck moments (unknown fields, failed navigation)."""
                if autopilot or not on_stuck:
                    return True
                try:
                    ss_path = f"output/ea_{user_id}_prefinal.png"
                    await page.screenshot(path=ss_path, full_page=False)
                    if on_screenshot:
                        await on_screenshot(ss_path)
                except Exception:
                    pass
                reply = (await on_stuck(
                    "Ready to submit? Reply *submit* to apply now, or *cancel* to stop."
                ) or "").strip().lower()
                return reply in ("submit", "yes", "y", "ok")

            step_num  = 0
            submitted = False

            while step_num < MAX_STEPS:
                await page.wait_for_timeout(1500)

                if not await is_modal_open(page):
                    submitted = await check_submitted(page)
                    break

                step_num += 1
                logger.info(f"\n  ── Step {step_num} ──")

                # Check if read-only review step
                try:
                    modal = page.locator(MODAL_SEL).first
                    modal_text = (await modal.inner_text() or "").lower() if await modal.count() > 0 else ""
                    is_review = any(p in modal_text for p in [
                        "review your application",
                        "review application",
                        "the employer will also receive",
                        "submit your application",
                        "review your info",
                        "application review",
                    ])
                except Exception:
                    is_review = False

                if is_review:
                    logger.info("  Review step — skipping fill")
                    if on_notify:
                        await on_notify("📋 Review step...")
                else:
                    if on_notify and step_num == 1:
                        await on_notify("📝 Filling your details...")
                    await fill_step(
                        page, profile, resume_path, user_id, on_stuck, result,
                        gemini_client=gemini_client, model=model, pro_model=pro_model,
                        resume_text=resume_text, whiteboard=wb, on_screenshot=on_screenshot,
                    )
                    # Send filled summary to user after each step
                    if on_notify and result.fields_filled:
                        last_filled = wb.steps[-1]["filled"] if wb.steps else []
                        if last_filled:
                            filled_txt = "\n".join(f"  ✅ {f}" for f in last_filled)
                            await on_notify(f"📝 *Step {step_num} filled:*\n{filled_txt}")

                # Screenshot each step
                async def take_step_screenshot():
                    try:
                        ss = f"output/ea_{user_id}_step{step_num}.png"
                        modal_el = page.locator(MODAL_SEL).first
                        if await modal_el.count() > 0:
                            await modal_el.screenshot(path=ss)
                        else:
                            await page.screenshot(path=ss)
                        result.screenshot_path = ss
                        if on_screenshot:
                            await on_screenshot(ss)
                        return ss
                    except Exception:
                        return ""

                await take_step_screenshot()

                # ── Human-in-loop review (supervised mode only) ───────────
                if on_stuck and not is_review and not autopilot:
                    for _fix_attempt in range(4):  # max 3 fix attempts per step
                        reply = (await on_stuck(
                            f"Step {step_num} filled. Reply *ok* to continue, "
                            f"or describe what to fix."
                        ) or "").strip().lower()

                        if reply in ("ok", "yes", "y", "next", "looks good", "good", ""):
                            break

                        # User wants a fix — ask Gemini to plan one action
                        if on_notify:
                            await on_notify(f"🔧 Fixing: _{reply}_")
                        try:
                            modal_el = page.locator(MODAL_SEL).first
                            modal_html = await modal_el.inner_html() if await modal_el.count() else ""
                            safe_profile = {k: v for k, v in profile.items() if k not in ("password",) and v}
                            fix_prompt = f"""A job application form step is currently displayed.
The user wants to fix: "{reply}"

Profile: {json.dumps(safe_profile, indent=2)}

Form HTML:
{modal_html[:6000]}

Return ONE browser action as JSON to fix exactly what the user described:
{{"action": "fill|click|click_option|clear_and_fill|press_sequentially", "selector": "css selector", "value": "new value", "label": "field name"}}
Return ONLY the JSON object, no markdown."""
                            fix_action = _llm_json(fix_prompt, None,
                                                   gemini_client, pro_model or model)
                            modal_el2 = page.locator(MODAL_SEL).first
                            await _dispatch_action(
                                page=page, modal=modal_el2, action=fix_action,
                                resume_path=resume_path, user_id=user_id,
                                result=result, gemini_client=gemini_client,
                                model=pro_model or model, whiteboard=None,
                                on_stuck=None, on_screenshot=None,
                                filled_selectors=set(),
                                profile=profile,
                            )
                            await page.wait_for_timeout(800)
                        except Exception as e:
                            logger.warning(f"  Fix action failed: {e}")

                        # Show updated screenshot after fix
                        await take_step_screenshot()

                # ── Determine next action ─────────────────────────────────
                # Find the button FIRST so the user can confirm BEFORE we
                # click Submit — a post-click confirmation can't cancel.
                btn, action = await find_next_button(page)

                if action == "submit" and not await confirm_submit():
                    result.status = "cancelled"
                    result.steps_completed = step_num
                    if on_notify:
                        await on_notify("❌ Application cancelled.")
                    break

                if btn is not None:
                    try:
                        await btn.click(timeout=5000)
                        logger.info(f"  Clicked {action}")
                    except Exception as e:
                        logger.info(f"  Click failed ({action}): {e}")
                        action = "none"
                await page.wait_for_timeout(2000)

                if action == "submit":
                    await page.wait_for_timeout(4000)
                    submitted = await check_submitted(page)
                    result.steps_completed = step_num
                    break

                elif action == "none":
                    logger.warning(f"  No Next/Submit on step {step_num}")
                    # ── Autonomous recovery: screenshot → Gemini diagnoses → fix ──
                    recovered = await autonomous_nav_recovery(
                        page, step_num, gemini_client, pro_model or model, on_screenshot, user_id
                    )
                    if recovered:
                        # Gemini fixed something — retry navigation
                        btn, action = await find_next_button(page)
                        if action == "submit" and not await confirm_submit():
                            result.status = "cancelled"
                            result.steps_completed = step_num
                            if on_notify:
                                await on_notify("❌ Application cancelled.")
                            break
                        if btn is not None:
                            try:
                                await btn.click(timeout=5000)
                            except Exception:
                                action = "none"
                        await page.wait_for_timeout(2000)
                        if action in ("next", "review", "submit"):
                            logger.info(f"  Recovery succeeded → {action}")
                            if action == "submit":
                                await page.wait_for_timeout(4000)
                                submitted = await check_submitted(page)
                                result.steps_completed = step_num
                                break
                            continue
                    # Still stuck — ask user with screenshot
                    if on_screenshot:
                        try:
                            ss = f"output/ea_{user_id}_stuck{step_num}.png"
                            await page.screenshot(path=ss, full_page=False)
                            await on_screenshot(ss)
                        except Exception:
                            pass
                    if on_stuck:
                        reply = await on_stuck(
                            f"Stuck on step {step_num} — can't find Next/Submit.\n"
                            "Reply 'retry' to try again or 'skip' to stop."
                        )
                        if reply and "retry" in reply.lower():
                            continue
                    break

            # Final status — never clobber an explicit user cancel
            if result.status != "cancelled":
                result.status = "success" if submitted else "failed"
                if not submitted and not result.error:
                    result.error = f"Completed {step_num} steps but no submit confirmation"
            result.steps_completed = step_num

            # Final screenshot
            try:
                ss = f"output/ea_{user_id}_done.png"
                await page.screenshot(path=ss, full_page=True)
                result.screenshot_path = ss
                if on_screenshot:
                    await on_screenshot(ss)
            except Exception:
                pass

        except Exception as e:
            import traceback as _tb
            result.status = "failed"
            result.error  = str(e) or repr(e) or type(e).__name__
            logger.error(f"  Easy Apply error: {result.error}\n{_tb.format_exc()}")
            try:
                ss = f"output/ea_{user_id}_error.png"
                await page.screenshot(path=ss)
                result.screenshot_path = ss
                if on_screenshot:
                    await on_screenshot(ss)
            except Exception:
                pass
        finally:
            from screencast import stop_screencast
            await stop_screencast(cast)
            await browser.close()

    logger.info(
        f"  Result: {result.status} | Steps: {result.steps_completed} | "
        f"Filled: {len(result.fields_filled)}"
    )
    wb.print_final_summary()
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    USER_ID = int(os.getenv("EA_USER_ID", "1"))  # matches dir in user_profiles/

    # Pick latest PDF in the user's profile dir; fall back to the demo resume
    _profile_dir = Path("user_profiles") / str(USER_ID)
    _pdfs = sorted(_profile_dir.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)
    DEFAULT_RESUME = str(_pdfs[0]) if _pdfs else "samples/demo_resume.pdf"

    if len(sys.argv) < 2:
        print("Usage: python linkedin_easy_apply.py <linkedin_job_url> [resume_pdf]")
        print("Example: python linkedin_easy_apply.py 'https://www.linkedin.com/jobs/view/4387980268'")
        sys.exit(1)

    job_url     = sys.argv[1]
    resume_path = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else DEFAULT_RESUME

    async def _on_stuck(q):
        try:
            return input(f"\n  ❓ '{q}'\n  Answer: ").strip()
        except EOFError:
            return "ok"

    async def _on_screenshot(path):
        print(f"  Screenshot → {path}")

    async def _on_notify(msg):
        print(f"  {msg}")

    # Gemini client
    from google import genai as _genai
    _gemini = _genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
    _model     = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")       # field identification
    _pro_model = os.getenv("GEMINI_PRO_MODEL", "gemini-3.5-flash")  # autonomous decisions

    print("\nLinkedIn Easy Apply — LIVE\n")
    print(f"  Profile : user_profiles/{USER_ID}/profile.json")
    print(f"  Resume  : {resume_path}\n")

    result = asyncio.run(run_easy_apply(
        job_url=job_url,
        resume_path=resume_path,
        user_id=USER_ID,
        gemini_client=_gemini,
        model=_model,
        pro_model=_pro_model,
        on_stuck=_on_stuck,
        on_screenshot=_on_screenshot,
        on_notify=_on_notify,
    ))

    print(f"\nStatus  : {result.status.upper()}")
    print(f"Steps   : {result.steps_completed}")
    print(f"Filled  : {result.fields_filled}")
    print(f"Learned : {result.fields_learned}")
    print(f"Skipped : {result.fields_skipped}")
    if result.error:
        print(f"Error   : {result.error}")
