"""
apply_engine.py — Unified, deterministic-first form-fill engine
================================================================
ONE engine for any Workday-style application page. Every field flows through:

    classify(element) -> interaction-type
    resolve(field)    -> answer   (cache -> deterministic -> LLM -> hold-for-user)
    apply(element)    -> Playwright action

and the page through converge_page():

    fill everything we can -> click Save & Continue -> if blocked, the page's
    OWN validation errors are the to-do list -> clear+refill exactly those ->
    repeat until it advances or a field genuinely needs the user.

The LLM is NOT a page planner (that was the old failure mode). It is only the
universal fallback resolver for "no deterministic answer", confidence-gated,
cached, with user-confirm.

Phasing (see plan luminous-dazzling-turing.md):
  Phase 1 (this file initially): text / select / date / upload /
           currently-here checkbox / phantom-row delete / error-correction /
           gateway + auth advance.  Radio/consent/sensitive/LLM-answer/password
           are CLASSIFIED but deferred to later phases (apply logs them).
"""

import os
import re
import glob
import base64

from urllib.parse import urlparse

from auto_agent import (collect_elements, execute_action, upload_in_frames,
                        settle, switch_if_new_tab, FLASH_MODEL, annotate_screenshot,
                        dismiss_overlays, clear_blocking_overlays)
from workday import WORKDAY_NEXT_BUTTON, WORKDAY_SUBMIT_BUTTON
from profile_manager import get_field_value, load_profile, save_profile
from apply_llm import llm_json


# Unattended auto-answer mode. When True (set by the orchestrator for an
# auto-submit run), the engine FILLS its best LLM answer for free-text questions
# and low-confidence dropdowns instead of holding for the user. Default off →
# the safe, human-in-the-loop behavior (notebooks, attended runs) is unchanged.
AUTO_ANSWER = False

import json as _json
import logging

_log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# v2 HTML-to-LLM form filler — modeled on linkedin_easy_apply.analyze_modal
# ═══════════════════════════════════════════════════════════════════════════════

_FORM_FILL_PROMPT = """You are a browser automation agent filling a job application form.

You will receive:
1. The HTML of the form on the current page
2. The applicant's profile JSON (source of truth for all values)
3. The applicant's resume text (fallback if profile is missing a value)

Return a JSON array of browser actions to fill EVERY field on this form.

Each action must have:
- "action": one of: fill, click, click_option, upload, press_sequentially, press_key, scroll_into_view, hover, clear_and_fill, select_native, wait
- "selector": CSS selector to find the element (prefer [name='x'] > [id='x'] > [aria-label='x'] > [data-automation-id='x'] > label text)
- "value": value to use (string), or null if not needed
- "label": human-readable field name (for logging)

Action meanings:
- fill              -> el.fill(value)  — for text/email/tel/number/textarea inputs
- click             -> el.click()      — for buttons, labels, radio options, checkboxes
- click_option      -> open a dropdown then click the matching option text (for custom combobox/listbox/react-select components); set value = option text
- select_native     -> el.select_option(label=value)  — ONLY for native <select> tags
- upload            -> set_input_files(value) — for file inputs; set value = "__RESUME__"
- press_sequentially -> type char by char — for autocomplete/typeahead inputs
- press_key         -> el.press(value) — e.g. "Enter", "Tab", "Escape"
- scroll_into_view  -> scroll element into view
- hover             -> hover over element
- clear_and_fill    -> clear then fill — for pre-populated inputs that need overwriting
- wait              -> wait N ms; value = "500"

Rules:
- Read the HTML carefully. Identify EVERY input, select, textarea, radio, checkbox that needs filling.
- For native <select> tags: use select_native (NOT click_option). Read the <option> values from the HTML.
- For custom dropdowns (role=combobox, role=listbox, class contains 'select'): use click_option.
- For radio groups: click the <label> or the radio <input> for the correct option.
- For checkboxes labeled with consent/terms/agreement/privacy/acknowledge: click to check them.
- For file inputs (input[type='file']): return upload with value = "__RESUME__". Never skip file inputs.
- For date fields: fill with the format matching the placeholder or the label hint (MM/YYYY, MM/DD/YYYY, etc.).
- For "how did you hear about us" / source / referral: pick "Company Website", "Career Site", "Job Board", or "LinkedIn" if available, else "Other".
- For EEO/ethnicity/race/veteran/disability questions: pick "Decline to self-identify" or "Prefer not to answer" if available.
- For yes/no questions about visa/sponsorship/work authorization: candidate is Indian citizen in India, answer "No" for sponsorship needed.
- For numeric experience fields: use whole numbers, round from profile (5.7 -> 6). Never return null.
- For notice period / "how soon can you join": convert from profile (e.g. "30 days" -> 30, "2 months" -> 60, "immediate" -> 0). Default to 30 if missing.
- Values MUST come from the profile or resume. NEVER invent unrelated values.
- For fields where the profile has no data AND you cannot reasonably derive an answer: set value = null.
- SKIP these — do NOT click or interact with them:
  * Navigation buttons: Next, Submit, Save, Continue, Back, Cancel
  * OAuth / SSO buttons: "Apply With LinkedIn", "Sign in with LinkedIn", "Sign in with Google", "Login with SSO", any social login button
  * Application method choosers: "Autofill with Resume", "Use My Last Application", "Apply Manually", "Upload Resume"
  * Header/footer links, sign-out, search bars, chat widgets, Follow Us, privacy policy links
- If the page has NO fillable form fields (only buttons like "Apply Manually", "Apply With LinkedIn"), return an EMPTY array [].
- Return ONLY a valid JSON array, no markdown, no explanation.

=== PROFILE JSON ===
{profile}

=== RESUME TEXT ===
{resume_text}

=== FORM HTML ===
{form_html}
"""


def _compact_html(html: str, max_chars: int = 60_000) -> str:
    """Strip SVG/style/script/comments and collapse whitespace."""
    html = re.sub(r"<(svg|style|script)\b.*?</\1>", "", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n", html)
    return html[:max_chars]


async def _get_form_html(page, max_chars: int = 60_000) -> str:
    """Find the best form container on the page and return compacted HTML.
    Tries <form>, <main>, [role='main'], then falls back to <body>."""
    for sel in ("form", "main", "[role='main']", "#content", ".content"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                html = await loc.inner_html()
                if len(html) > 300:
                    print(f"  v2: scoped to <{sel}> ({len(html)} chars raw)")
                    return _compact_html(html, max_chars)
        except Exception:
            continue
    try:
        html = await page.inner_html("body")
        print(f"  v2: fell back to <body> ({len(html)} chars raw)")
        return _compact_html(html, max_chars)
    except Exception:
        return ""


async def _analyze_form(page, profile: dict, gemini_client=None) -> list:
    """Send compacted form HTML + profile to the LLM. Returns list of actions."""
    form_html = await _get_form_html(page)
    if not form_html or len(form_html) < 50:
        print("  v2: form HTML too short, skipping")
        return []

    safe_profile = {k: v for k, v in profile.items()
                    if k not in ("password", "_resolved", "_dropdown_resolved",
                                 "_llm_resolved") and v}
    resume_text = str(profile.get("_resume_text", ""))[:5000]

    prompt = _FORM_FILL_PROMPT.format(
        profile=_json.dumps(safe_profile, indent=2, ensure_ascii=False),
        resume_text=resume_text or "(no resume text available)",
        form_html=form_html,
    )

    try:
        data = llm_json(
            prompt + '\n\nIf you must return a JSON object, wrap the array as {"actions": [...]}.',
            gemini_client=gemini_client, gemini_model=FLASH_MODEL,
        )
        if isinstance(data, dict):
            data = data.get("actions") or next(
                (v for v in data.values() if isinstance(v, list)), [])
        if isinstance(data, list):
            print(f"  v2: LLM planned {len(data)} actions")
            return data
    except Exception as e:
        print(f"  v2: form analysis failed: {e}")
    return []


async def _dismiss_autocomplete(page, value: str):
    """If a listbox/dropdown appeared after typing, click the best match.
    Lightweight — no LLM, no screenshot. Just DOM pattern matching."""
    try:
        listbox = page.locator("[role='listbox']:visible, [role='menu']:visible, "
                               "ul.suggestions:visible, .autocomplete-results:visible").first
        if await listbox.count() == 0:
            return
        opts = listbox.locator("[role='option'], li")
        count = await opts.count()
        if count == 0:
            return
        val_lower = value.lower()
        for i in range(min(count, 15)):
            try:
                text = ((await opts.nth(i).inner_text()) or "").strip()
                if text and (val_lower in text.lower() or text.lower() in val_lower):
                    await opts.nth(i).click()
                    print(f"    v2 autocomplete: clicked '{text[:40]}'")
                    await page.wait_for_timeout(300)
                    return
            except Exception:
                continue
        # No match — press Escape to close the dropdown
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
    except Exception:
        pass


async def _form_dispatch(page, action: dict, resume_path: str) -> str:
    """Execute one CSS-selector-based action. Returns 'filled'|'skipped'|'failed'.

    Ported from linkedin_easy_apply._dispatch_action — handles the same action
    types but scoped to the full page (no modal wrapper)."""
    act = (action.get("action") or "").strip().lower()
    selector = (action.get("selector") or "").strip()
    value = action.get("value")
    label = action.get("label") or selector or act

    if not act:
        return "skipped"

    # wait — no element needed
    if act == "wait":
        try:
            ms = int(re.sub(r"[^0-9]", "", str(value or "")) or 500)
        except Exception:
            ms = 500
        await page.wait_for_timeout(min(ms, 10_000))
        return "skipped"

    if not selector:
        print(f"    v2 skip (no selector): [{label}]")
        return "skipped"

    # Resolve element
    el = None
    try:
        candidate = page.locator(selector).first
        if await candidate.count() > 0:
            el = candidate
    except Exception:
        pass

    if el is None:
        print(f"    v2 miss: {selector} [{label}]")
        return "failed"

    needs_value = act in ("fill", "click_option", "select_native", "upload",
                          "press_sequentially", "clear_and_fill")
    if needs_value and (value is None or str(value).strip() == ""):
        print(f"    v2 skip (null value): [{label}]")
        return "skipped"

    try:
        if act == "scroll_into_view":
            await el.scroll_into_view_if_needed()
            return "skipped"

        elif act == "hover":
            await el.hover()
            return "skipped"

        elif act == "press_key":
            await el.press(str(value))
            return "filled"

        elif act == "upload":
            path = resume_path
            if path and os.path.exists(path):
                await el.set_input_files(path)
                print(f"    v2 uploaded: {os.path.basename(path)} [{label}]")
                await page.wait_for_timeout(800)
                return "filled"
            else:
                print(f"    v2 skip: no resume file for [{label}]")
                return "skipped"

        elif act == "fill":
            # Already-filled guard: the v2 pass re-emits every field each loop, so
            # without this it keeps re-typing First/Last/Email that are already
            # correct (the "why does it refill?" churn). Skip if the input already
            # holds the target value.
            try:
                cur = (await el.input_value()) or ""
                if cur.strip() and cur.strip().lower() == str(value).strip().lower():
                    return "skipped"
            except Exception:
                pass
            await el.scroll_into_view_if_needed()
            await el.fill(str(value))
            print(f"    v2 filled: {label} = {str(value)[:40]}")
            await page.wait_for_timeout(400)
            await _dismiss_autocomplete(page, str(value))
            return "filled"

        elif act == "clear_and_fill":
            await el.scroll_into_view_if_needed()
            await el.click(click_count=3)
            await el.press("Control+a")
            await el.press("Backspace")
            await el.fill(str(value))
            print(f"    v2 clear+fill: {label} = {str(value)[:40]}")
            await page.wait_for_timeout(400)
            return "filled"

        elif act == "press_sequentially":
            await el.scroll_into_view_if_needed()
            await el.click(click_count=3)
            await el.press_sequentially(str(value), delay=60)
            print(f"    v2 typed: {label} = {str(value)[:40]}")
            await page.wait_for_timeout(600)
            await _dismiss_autocomplete(page, str(value))
            return "filled"

        elif act == "click":
            await el.scroll_into_view_if_needed()
            await el.click()
            print(f"    v2 clicked: {label}")
            await page.wait_for_timeout(300)
            return "filled"

        elif act == "select_native":
            await el.select_option(label=str(value), timeout=5000)
            print(f"    v2 native select: {label} = {str(value)[:40]}")
            await page.wait_for_timeout(300)
            return "filled"

        elif act == "click_option":
            await el.scroll_into_view_if_needed()
            # Check if it's a native <select> first
            try:
                tag = (await el.evaluate("e => e.tagName")).lower()
            except Exception:
                tag = ""
            if tag == "select":
                try:
                    await el.select_option(label=str(value), timeout=5000)
                    print(f"    v2 select (native): {label} = {str(value)[:40]}")
                    return "filled"
                except Exception:
                    pass

            # Custom dropdown: click to open, find matching option
            await el.click()
            await page.wait_for_timeout(600)

            option_found = False
            val_lower = str(value).lower()
            val_escaped = str(value).replace('"', '\\"')
            for list_sel in [
                f'[role="option"]:has-text("{val_escaped}")',
                f'li:has-text("{val_escaped}")',
                "[role='listbox'] [role='option']",
                "ul[role='listbox'] li",
                "[data-automation-id='promptOption']",
                ".select__option",
            ]:
                try:
                    opts = page.locator(list_sel)
                    count = await opts.count()
                    if count == 0:
                        continue
                    for i in range(min(count, 25)):
                        opt_text = ((await opts.nth(i).inner_text()) or "").strip()
                        if not opt_text:
                            continue
                        if (val_lower in opt_text.lower()
                                or opt_text.lower() in val_lower):
                            await opts.nth(i).click()
                            print(f"    v2 click_option: {label} = {opt_text[:40]}")
                            option_found = True
                            await page.wait_for_timeout(400)
                            break
                    if option_found:
                        break
                except Exception:
                    continue

            if not option_found:
                # Fallback: type into the field and press Enter (typeahead)
                try:
                    await el.fill(str(value))
                    await page.wait_for_timeout(500)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(400)
                    print(f"    v2 click_option fallback (type+enter): {label} = {str(value)[:40]}")
                    return "filled"
                except Exception:
                    print(f"    v2 click_option: no match for '{value}' [{label}]")
                    return "failed"
            return "filled"

        else:
            print(f"    v2 unknown action '{act}' [{label}]")
            return "skipped"

    except Exception as e:
        print(f"    v2 error [{act}|{label}]: {e}")
        return "failed"


async def _fill_pass_v2(page, profile, user_id, creds, resume_path,
                        upload_state, gemini_client, held_list, fill_memo=None):
    """v2 fill pass: HTML -> LLM -> CSS-selector actions -> dispatch.
    Returns the same bucket dict as _fill_pass for backward compatibility."""
    # Quick check: if the page has very few form inputs, it's probably a
    # gateway/choice page (e.g. "Apply Manually" / "Apply With LinkedIn"),
    # not a real form. Skip v2 so the orchestrator handles it normally.
    try:
        n_inputs = await page.evaluate(
            "document.querySelectorAll('input:not([type=hidden]),select,textarea').length")
        if n_inputs < 2:
            print(f"  v2: only {n_inputs} inputs on page — likely a gateway, skipping")
            return None
    except Exception:
        pass

    buckets = {"filled": [], "nodata": [], "fail": [], "held": []}

    actions = await _analyze_form(page, profile, gemini_client)
    if not actions:
        print("  v2: no actions from LLM — falling back to v1")
        return None  # signal caller to fall back to v1

    for action in actions:
        label = action.get("label") or action.get("selector") or "?"
        result = await _form_dispatch(page, action, resume_path)

        if result == "filled":
            buckets["filled"].append(label)
            if action.get("action") == "upload":
                upload_state["done"] = True
        elif result == "failed":
            buckets["fail"].append(label)

        await page.wait_for_timeout(120)

    return buckets


# ═══════════════════════════════════════════════════════════════════════════════
# End of v2 HTML-to-LLM fill — everything below is v1 (kept for error
# correction, grounded repair, phantom rows, and as fallback)
# ═══════════════════════════════════════════════════════════════════════════════


# ── small helpers ────────────────────────────────────────────────────────────
_SEC_LIST = {"work experience": "experience", "employment": "experience",
             "education": "education", "certification": "certifications",
             "certifications": "certifications", "language": "languages"}
_SEC_RE = re.compile(
    r"(Work Experience|Employment|Education|Certifications?|Languages?)\s+(\d+)", re.I)

def _parse_sec(sl):
    m = _SEC_RE.search(sl or "")
    if not m:
        return None
    key = _SEC_LIST.get(m.group(1).lower()) or _SEC_LIST.get(m.group(1).lower().rstrip("s"))
    return (key, int(m.group(2)) - 1) if key else None


def _assign_section_rows(elements):
    """Give un-numbered repeated sections a numbered section_label.

    Some ATSes (e.g. HPE) render ONE 'Work Experience :' header over several
    un-numbered rows. collect_elements tags those fields with `section_word`
    (the bare word) but no `section_label`, so get_field_value's row tier can't
    map them. Here we split each section_word group into rows by DOM order —
    a new row starts when a field label repeats — and synthesize
    'Work Experience 1', 'Work Experience 2', ... so the existing row tier maps
    each row to profile.<list>[i]. No-op for rows that already carry a DOM
    section_label (numbered tenants like Workday) and for single-row pages.
    Mutates and returns `elements`."""
    counters, seen = {}, {}
    for e in elements:
        sw = (e.get("section_word") or "").strip()
        if not sw or e.get("section_label"):
            continue
        lab = re.sub(r"\s+", " ", (e.get("label") or "").lower().replace("*", "")).strip()
        if sw not in counters:
            counters[sw], seen[sw] = 0, set()
        if lab and lab in seen[sw]:          # label repeats -> next row
            counters[sw] += 1
            seen[sw] = set()
        if lab:
            seen[sw].add(lab)
        e["section_label"] = f"{sw} {counters[sw] + 1}"
        e["row_index"] = counters[sw]
    return elements

def _row_is_current(sl, profile):
    p = _parse_sec(sl)
    if not p:
        return None
    key, i0 = p
    entries = profile.get(key) or []
    if 0 <= i0 < len(entries):
        return str(entries[i0].get("is_current")).strip().lower() in ("true", "1", "yes")
    return None

def _is_date_label(label):
    l = (label or "").lower().strip()
    toks = set(re.findall(r"[a-z0-9]+", l))
    if l in ("from", "to") or l.startswith("from ") or l.startswith("to "):
        return True
    if "mm/yyyy" in l or "mm / yyyy" in l or "mm/dd/yyyy" in l:
        return True
    return "date" in toks

def _is_placeholder_value(v):
    s = str(v or "").strip().lower().strip("-").strip()
    if s in ("", "select one", "please select", "select", "select...",
             "select one...", "select an option", "choose", "choose one",
             "choose...", "none selected", "mm/yyyy", "mm / yyyy",
             "mm/dd/yyyy", "dd/mm/yyyy", "yyyy"):
        return True
    # "Please select ...", "Select a country", "-- Select --", etc.
    if s.startswith("please select") or s.startswith("select a") or s.startswith("select an"):
        return True
    return bool(re.match(r"^(mm|dd|yyyy)\s*[\/\-.]", s))

def default_resume(profile):
    p = profile.get("resume_file")
    if p and os.path.exists(str(p)):
        return str(p)
    cands = []
    for pat in ("*.pdf", "*.PDF", "*.docx", "*.DOCX"):
        cands.extend(glob.glob(os.path.join(os.getcwd(), "temp_resumes", pat)))
    return max(cands, key=os.path.getmtime) if cands else ""


# ── resolution cache (persisted to profile["_resolved"]) ─────────────────────
def _ck(label, val):
    return f"{(label or '').lower().strip()}|{str(val or '').lower().strip()}"

def _cache_get(profile, label, val):
    # Bug A fix: check BOTH cache dicts (the engine writes _resolved; the old
    # notebook wrote _dropdown_resolved). `A or B` skipped B once A was non-empty.
    # Bug C fix: also try the normalized "stem" key, so a dropdown label that
    # embeds its current value ("Degree Master's ... Required") still matches the
    # stem cached when the field was empty ("Degree Select One").
    keys = (_ck(label, val), _ck_norm(label, val))
    for d in (profile.get("_resolved"), profile.get("_dropdown_resolved")):
        if not d:
            continue
        for k in keys:
            if k in d:
                return d[k]
    return None

def _cache_set(profile, user_id, label, val, option):
    # Poison guard: never persist an option unrelated to the intended value
    # (a mis-snapped multiselect once cached 'Asian' for a decline answer and
    # re-broke every later run on that form).
    if val and option and not _related(val, option):
        print(f"     (cache skipped: {option!r} unrelated to {val!r})")
        return
    r = profile.setdefault("_resolved", {})
    r[_ck(label, val)] = option          # raw (exact, backward compatible)
    r[_ck_norm(label, val)] = option     # normalized stem (stable across runs)
    try:
        save_profile(user_id, profile)
    except Exception as ex:
        print(f"     (cache persist failed: {ex})")


# ── classification ───────────────────────────────────────────────────────────
_NAV_SKIP = ("careers home", "search for jobs", "candidate home", "job alerts",
             "sign out", "log out", "back to job posting", "privacy",
             "settings", "english", "main menu", "skip to", "drop off",
             "read full", "decline cookies", "accept cookies", "follow us",
             "save and continue", "submit", "back", "delete", "remove", "cancel",
             "add another", "add ", "errors and alerts", "error-", "alert-")

_CONSENT_KEYWORDS = ("agree", "consent", "terms", "privacy", "have read",
                     "i confirm", "i acknowledge", "i accept", "policy",
                     "disclosure", "disclaimer", "certify", "authorize the")

def _is_consent_label(label):
    s = (label or "").lower()
    return any(k in s for k in _CONSENT_KEYWORDS)

def classify_field(e):
    """Map an element record -> interaction type."""
    tag     = e.get("tag")
    typ     = (e.get("type") or "")
    control = (e.get("control") or "")        # 'checkbox' | 'radio' | '' (from _COLLECT_JS)
    widget  = e.get("widget")
    label   = (e.get("label") or "").lower().strip()
    opt     = (e.get("option") or "").lower()
    q       = (e.get("q") or "").lower()
    is_checkbox = control == "checkbox" or typ == "checkbox"
    is_radio    = control == "radio" or typ == "radio"

    # Upload widget.
    if any(k in label for k in ("select files", "drop files", "upload a file",
                                "upload resume", "attach", "upload your")):
        return "upload"
    # 'I currently work/study here' checkbox.
    if is_checkbox and ("currently work" in label or "currently study" in label
                        or "currently work" in opt or "currently study" in opt):
        return "current_checkbox"
    # Consent / acknowledgment checkbox — check label, option, AND fieldset (q),
    # because Workday often puts the consent text in a sibling, not the label.
    if is_checkbox and (_is_consent_label(label) or _is_consent_label(opt)
                        or _is_consent_label(q)):
        return "consent"
    # Radio group member (each option is its own element; has `option` + `q`).
    if is_radio:
        return "radio"
    # Any other checkbox.
    if is_checkbox:
        return "checkbox_other"
    # Skills / multi-value typeahead chip-list. MUST come before the nav-skip
    # check, because "Type to Add Skills" contains "add " (a nav-skip token).
    if "skill" in label and (widget == "typeahead" or tag == "input"):
        return "chiplist"
    # Explicit nav / control skip.
    if any(k in label for k in _NAV_SKIP):
        return "skip"
    # Date fields.
    if _is_date_label(label) and tag in ("input", "button", "textarea"):
        return "date"
    # Dropdowns.
    if tag == "select" or widget in ("select", "typeahead") or e.get("options"):
        return "select"
    # Plain text / textarea.
    if tag in ("input", "textarea") and typ not in ("hidden", "file", "button", "submit"):
        return "text"
    return "skip"


# ── option matching + dropdown scrape ────────────────────────────────────────
_OPT_JS = r"""
(() => {
    const sels = ['[data-automation-id="promptOption"]','[data-automation-id="promptLeafNode"]',
        '[role="listbox"] [role="option"]','[role="listbox"] li','[role="option"]',
        'ul[role="listbox"] li','li[role="option"]'];
    const popups = Array.from(document.querySelectorAll(
        '[role="listbox"],[role="menu"],[data-automation-id*="opup"]'))
        .filter(p => { const r=p.getBoundingClientRect(); return r.width>1&&r.height>1; });
    const root = popups.length ? popups[popups.length-1] : document;
    const seen = [];
    root.querySelectorAll(sels.join(', ')).forEach(e => {
        const r=e.getBoundingClientRect(); if(r.width<=1||r.height<=1) return;
        const t=(e.innerText||e.textContent||'').replace(/\s+/g,' ').trim();
        if(t && t.length<=200 && t.toLowerCase()!=='select one' && seen.indexOf(t)<0) seen.push(t);
    });
    return seen;
})()
"""

import unicodedata as _ud
def _deaccent(s):
    """'Karnātaka' -> 'karnataka' (strip diacritics + lowercase)."""
    return "".join(c for c in _ud.normalize("NFKD", str(s or ""))
                   if not _ud.combining(c)).lower().strip()

def _key_stem(label):
    """Stable field-name stem for a dropdown cache key. Workday dropdown labels
    often embed the current value / 'Select One' / 'Required' / '*', so the raw
    label changes once a value is picked. Strip that noise so the key is the
    same whether the field is empty or filled (e.g. 'Degree Select One Required'
    and 'Degree Master's ...' both -> 'degree')."""
    s = _deaccent(label)                       # lower + strip accents
    for noise in ("(required)", "required", "select one"):
        s = s.replace(noise, " ")
    s = s.replace("*", " ")
    return re.sub(r"\s+", " ", s).strip()

def _ck_norm(label, val):
    """Normalized (accent- and noise-insensitive) cache key."""
    return f"{_key_stem(label)}|{_deaccent(val)}"

def _migrate_cache(profile):
    """One-time, idempotent: re-key legacy cache entries under the stable stem.
    Old keys often baked the SELECTED OPTION into the label (e.g.
    'degree master's / graduate degree ... required|mtech'), so they never
    matched the label seen on an EMPTY dropdown ('degree select one|mtech').
    Here we DO know the stored option (the dict value), so we can strip it out
    of the label and store an extra stem key ('degree|mtech') that matches both.
    Only ADDS keys (never deletes), so it can't break an existing hit. Returns
    True if anything was added."""
    r = profile.setdefault("_resolved", {})
    added = False
    for src in ("_resolved", "_dropdown_resolved"):
        d = profile.get(src) or {}
        for k, opt in list(d.items()):
            if "|" not in k:
                continue
            lab, val = k.rsplit("|", 1)
            lab2 = _deaccent(lab)
            # Drop the embedded option text out of the label, but ONLY when it's
            # substantial (>=5 chars). A short option like 'No'/'Yes' is a
            # substring of ordinary words ('No' in 'notice') and blind-stripping
            # it corrupts the stem.
            opt_d = _deaccent(opt)
            if len(opt_d) >= 5 and opt_d in lab2:
                lab2 = lab2.replace(opt_d, " ")
            nk = f"{_key_stem(lab2)}|{_deaccent(val)}"
            if nk not in r:
                r[nk] = opt
                added = True
    return added

def _strong_match(want, opts):
    """Match `want` against options, accent-insensitive. Returns the option."""
    if not want or not opts:
        return None
    w = _deaccent(want)
    for o in opts:
        if _deaccent(o) == w:
            return o
    for o in opts:
        ol = _deaccent(o)
        if ol.startswith(w) or w.startswith(ol):
            return o
    wt = set(re.findall(r"[a-z0-9]+", w))
    if wt:
        for o in opts:
            ot = set(re.findall(r"[a-z0-9]+", _deaccent(o)))
            if wt.issubset(ot):
                return o
    return None

async def _scrape_options(page, idx, type_hint=None):
    """Open dropdown [idx] and scrape its options. If `type_hint` is given and
    the desired value isn't in the initially-rendered (often virtualized) list,
    TYPE a search term to filter — required for long typeaheads like the 200+
    country-code list, where India only appears after you type 'India'."""
    sel = f'[data-agent-idx="{idx}"]'
    def _clean(raw):
        out = []
        for o in (raw or []):
            o = (o or "").strip()
            if o and o.lower() != "select one" and o not in out:
                out.append(o)
        return out
    try:
        loc = page.locator(sel).first
        await loc.scroll_into_view_if_needed(timeout=2000)
        await loc.click(timeout=3000, force=True)
        await page.wait_for_timeout(600)
        opts = _clean(await page.evaluate(_OPT_JS) or [])

        if type_hint:
            need = str(type_hint).lower()
            words = re.findall(r"[a-zA-Z]{3,}", str(type_hint))
            present = any(need in o.lower() or any(w.lower() in o.lower() for w in words)
                          for o in opts)
            if not present and not opts:
                # Only type to filter when we have NO options at all (virtualized
                # list). If we already scraped options (even non-matching ones like
                # "0-2"/"3-8"/"9+"), typing the hint ("years") into a non-searchable
                # dropdown just corrupts its state.
                term = words[0] if words else str(type_hint)
                try:
                    await page.keyboard.type(term, delay=45)
                    await page.wait_for_timeout(750)
                    opts2 = _clean(await page.evaluate(_OPT_JS) or [])
                    if opts2:
                        opts = opts2
                except Exception:
                    pass
        try: await page.keyboard.press("Escape")
        except Exception: pass
        await page.wait_for_timeout(150)
        return opts
    except Exception as ex:
        print(f"     (scrape options [{idx}] failed: {ex})")
        return []

# ── sensitive (EEO) detection + decline rule ─────────────────────────────────
_SENSITIVE = ("ethnic", "race", "hispanic", "latino", "veteran",
              "disabilit", "disabled", "protected")
def _is_sensitive_label(s):
    s = (s or "").lower()
    return any(k in s for k in _SENSITIVE)

_DECLINE = ("decline", "prefer not", "do not wish", "don't wish", "not to answer",
            "choose not", "i don't want", "i do not want", "not to disclose",
            "do not wish to", "i prefer not")
def _find_decline_option(options):
    for o in options:
        if any(k in o.lower() for k in _DECLINE):
            return o
    return None


# ── "How did you hear about us?" / source / referral — neutral default ───────
# Required on almost every ATS, never present in a profile, and not truly
# sensitive — so a low-confidence LLM guess would HOLD and stall the page on a
# required dropdown. Instead pick a neutral, always-acceptable answer.
_SOURCE_LABELS = ("how did you hear", "how do you hear", "how did you find",
                  "where did you hear", "referral source", "source of",
                  "lead source", "how were you referred")
def _is_source_label(s):
    s = (s or "").lower()
    if "source" in s and "open source" not in s:
        return True
    return any(k in s for k in _SOURCE_LABELS)

# First option matching any substring in the earliest group wins. Substrings are
# SPECIFIC on purpose (e.g. "career site", not bare "career") so we land on the
# company's own site and not "Career/Job Fair".
_SOURCE_PREF = (
    ("career site", "careers site", "career page", "careers page",
     "career website", "careers website", "career webpage", "careers webpage",
     "company website", "company site", "company webpage", "company web",
     "corporate website", "employer website"),
    ("job board", "linkedin", "indeed", "glassdoor"),
    ("online", "internet", "web search", "search engine", "direct"),
    ("other",),
)
def _find_source_option(options):
    low = [(o, o.lower()) for o in options]
    for group in _SOURCE_PREF:
        for o, ol in low:
            if any(k in ol for k in group):
                return o
    return None


# ── trimmed profile context for LLM (no heavy / internal keys) ───────────────
def _profile_ctx(profile):
    skip = ("_resume_text", "_resolved", "_dropdown_resolved", "_llm_resolved",
            "password", "screening")
    import json as _json
    ctx = {k: v for k, v in profile.items() if k not in skip}
    return _json.dumps(ctx, ensure_ascii=False)[:4000]


async def _llm_choose(question, options, profile, gemini_client, value_hint=None):
    """Pick the best option for a choice field (dropdown/radio). Uses the
    profile context so it can ANSWER questions (work-auth, relocate, etc.),
    not just map a known value. Returns (option_text, confidence)."""
    numbered = "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))
    hint = f'\nUSER\'S KNOWN VALUE FOR THIS FIELD: "{value_hint}"' if value_hint else ""
    prompt = (
        "Choose the single best option to answer ONE job-application field for this "
        "candidate.\n"
        f'FIELD / QUESTION: "{question}"{hint}\n'
        f"CANDIDATE PROFILE (JSON):\n{_profile_ctx(profile)}\n\n"
        f"OPTIONS:\n{numbered}\n\n"
        'Return STRICT JSON: {"option": "<exact option text copied verbatim from the '
        'list, or empty>", "confidence": "high|low"}\n'
        '- high = the option clearly and correctly answers for this candidate '
        '(e.g. "MTech"->a Master\'s option; authorized-to-work=Yes if profile says so).\n'
        '- For "have you ever / have you previously / do you have a <record>" style '
        'questions where the profile shows NO evidence of it (e.g. no prior employment '
        'at this company, no criminal record), choose the negative option ("No"/"None") '
        'with HIGH confidence — absence of evidence is a confident "No".\n'
        "- low = genuinely ambiguous, sensitive (EEO), or not derivable from the profile.")
    try:
        out = llm_json(prompt, gemini_client=gemini_client, gemini_model=FLASH_MODEL)
    except Exception as ex:
        print(f"     (LLM choose failed: {ex})")
        return "", "low"
    return ((out or {}).get("option") or "").strip(), ((out or {}).get("confidence") or "low").strip().lower()


async def _llm_draft(question, profile, gemini_client):
    """Draft a free-text answer from the profile. Always held for user review."""
    prompt = (
        "Draft a concise, professional answer to this job-application free-text "
        "question, in first person, using ONLY facts from the candidate profile. "
        "2-4 sentences. No placeholders.\n"
        f'QUESTION: "{question}"\n'
        f"CANDIDATE PROFILE (JSON):\n{_profile_ctx(profile)}\n\n"
        'Return STRICT JSON: {"answer": "<draft>"}')
    try:
        out = llm_json(prompt, gemini_client=gemini_client, gemini_model=FLASH_MODEL)
        return ((out or {}).get("answer") or "").strip()
    except Exception as ex:
        print(f"     (LLM draft failed: {ex})")
        return ""


# ── resolution result ────────────────────────────────────────────────────────
class Resolution:
    __slots__ = ("value", "source", "held", "suggestion")
    def __init__(self, value=None, source="missing", held=False, suggestion=None):
        self.value = value; self.source = source; self.held = held; self.suggestion = suggestion


async def _resolve_choice_core(idx, label, profile_value, options, profile,
                               user_id, gemini_client, held_list):
    """Unified resolver for ANY choice field (select OR radio), given the real
    options. cache -> sensitive-decline -> strong match -> LLM -> hold."""
    if not options:
        return Resolution(profile_value, "raw (no options)")
    # Sensitive (EEO) with no known value -> pick the 'decline to answer' option.
    # NEVER falls through to the LLM (it would guess demographics, e.g. 'Asian'
    # from an Indian profile). If no decline option is visible (virtualized /
    # empty scrape), use the near-universal EEO decline text — the typeahead
    # snaps to it.
    if not profile_value and _is_sensitive_label(label):
        d = _find_decline_option(options)
        return Resolution(d or "Decline to self identify", "decline" if d else "decline-default")
    # Cache (keyed by label|value, or label|"" for value-less questions).
    cached = _cache_get(profile, label, profile_value or "")
    if cached:
        m = _strong_match(cached, options) or (cached if cached in options else None)
        if m:
            return Resolution(m, "cached")
    # Strong string match of a known profile value.
    if profile_value:
        m = _strong_match(profile_value, options)
        if m:
            _cache_set(profile, user_id, label, profile_value, m)
            return Resolution(m, "exact")
    # "How did you hear about us?" / source — no profile value: pick a neutral
    # acceptable default (company career site -> job board -> direct -> Other)
    # so a REQUIRED source dropdown auto-fills instead of holding + blocking the
    # page. After cache + profile match, so a user-set value (resolve_field) wins.
    if not profile_value and _is_source_label(label):
        s = _find_source_option(options)
        if s:
            return Resolution(s, "source-default")
    # LLM choose (answers from profile when there's no known value).
    pick, conf = await _llm_choose(label, options, profile, gemini_client, value_hint=profile_value)
    pm = _strong_match(pick, options) if pick else None
    if pm and conf == "high":
        _cache_set(profile, user_id, label, profile_value or "", pm)
        return Resolution(pm, "llm-high")
    # Unattended auto-answer: accept the best guess even at low confidence rather
    # than blocking the run. (Off by default → attended runs still hold.)
    if AUTO_ANSWER and pm:
        _cache_set(profile, user_id, label, profile_value or "", pm)
        return Resolution(pm, "llm-auto")
    # Hold for user confirmation.
    print(f"\n  [CONFIRM NEEDED] choice [{idx}] {label[:60]!r}")
    if profile_value:
        print(f"     profile value : {profile_value!r}")
    if pick:
        print(f"     LLM guess     : {pick!r}  (confidence: {conf})")
    print(f"     real options  :")
    for i, o in enumerate(options, 1):
        print(f"        {i:2}. {o}")
    sugg = pick if pm else (options[0] if options else "<option>")
    print(f"     -> lock a choice in a NEW cell, then re-run:")
    print(f"        resolve_field(profile, {label!r}, {profile_value or ''!r}, {sugg!r}); save_profile({user_id}, profile)")
    held_list.append({"idx": idx, "label": label, "value": profile_value or "",
                      "suggestion": sugg, "kind": "choice"})
    return Resolution(None, "held", held=True, suggestion=sugg)


async def resolve_choice(page, e, val, profile, user_id, gemini_client, held_list):
    """Dropdown/select: scrape the real options (typing `val` to filter long
    typeaheads), then resolve via the core."""
    idx, label = e.get("idx"), e.get("label", "")
    opts = await _scrape_options(page, idx, type_hint=val)
    if not opts:
        return Resolution(val, "raw (no scrape)")
    return await _resolve_choice_core(idx, label, val, opts, profile, user_id, gemini_client, held_list)


def resolve_field_value(e, profile):
    """Deterministic profile value for this element. None = no data."""
    label = e.get("label", "")
    sl = e.get("section_label") or None
    v = get_field_value(label, profile, section_label=sl)
    if v is None and e.get("name"):
        # The visible label can be mis-derived (e.g. an intl-phone number input
        # that picks up the adjacent "+91" as its label). Fall back to the field's
        # name/id attribute: 'phone_number_field_number' -> 'phone number ...' -> phone.
        v = get_field_value(str(e["name"]).replace("_", " ").replace("-", " "), profile)
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("none", "null"):
        return None
    return s


def resolve_field(profile, label, value, option):
    """USER helper: lock in a held field's answer, then re-run.
       e.g. resolve_field(profile, 'Field of Study', 'Data Science', 'Data Science / Analytics')"""
    r = profile.setdefault("_resolved", {})
    r[_ck(label, value)] = option            # raw (exact)
    r[_ck_norm(label, value)] = option       # normalized stem (stable across runs)
    print(f"cached: {label!r} + {value!r} -> {option!r}.  Re-run converge_page.")


# ── date filler (inner-spinner -> keyboard) ──────────────────────────────────
async def _fill_date(page, idx, val):
    sel = f'[data-agent-idx="{idx}"]'
    val = str(val).strip()
    parts = [p for p in val.split("/") if p]
    # Strategy 1: Workday inner spinner inputs.
    try:
        loc = page.locator(sel).first
        await loc.scroll_into_view_if_needed(timeout=2000)
        await loc.click(timeout=3000, force=True)
        await page.wait_for_timeout(220)
        if len(parts) == 2:
            slots = {"dateSectionMonth-input": parts[0], "dateSectionYear-input": parts[1]}
        elif len(parts) == 3:
            slots = {"dateSectionMonth-input": parts[0], "dateSectionDay-input": parts[1],
                     "dateSectionYear-input": parts[2]}
        else:
            slots = {}
        filled_any = False
        for aid, txt in slots.items():
            inp = page.locator(f"{sel} [data-automation-id='{aid}'], [data-automation-id='{aid}']")
            if await inp.count() > 0:
                try:
                    await inp.first.fill(txt, timeout=2000); filled_any = True
                except Exception:
                    try:
                        await inp.first.click(timeout=1500)
                        await page.keyboard.type(txt, delay=30); filled_any = True
                    except Exception:
                        pass
        if filled_any:
            try: await page.keyboard.press("Tab")
            except Exception: pass
            return True
    except Exception:
        pass
    # Strategy 2: focus + keyboard type.
    try:
        await page.locator(sel).first.click(timeout=2000, force=True)
        await page.wait_for_timeout(180)
        try: await page.keyboard.press("Control+A")
        except Exception: pass
        await page.keyboard.type(val, delay=40)
        await page.keyboard.press("Tab")
        return True
    except Exception:
        return False


async def _clear_text(page, idx):
    try:
        loc = page.locator(f'[data-agent-idx="{idx}"]').first
        await loc.click(timeout=3000, force=True)
        await page.keyboard.press("Control+A"); await page.keyboard.press("Backspace")
        await page.wait_for_timeout(120)
    except Exception:
        pass


async def _toggle_check(page, idx):
    """Robustly CHECK a checkbox (input or div role=checkbox). Verifies state
    via .checked / aria-checked. Tries click -> Space -> parent-label click."""
    sel = f'[data-agent-idx="{idx}"]'
    loc = page.locator(sel).first
    try:
        await loc.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    async def _checked():
        try:
            return await loc.evaluate(
                "e => e.checked === true || e.getAttribute('aria-checked') === 'true'")
        except Exception:
            return False
    if await _checked():
        return True
    for how in ("click", "space", "label"):
        try:
            if how == "click":
                await loc.click(timeout=3000, force=True)
            elif how == "space":
                await loc.focus(); await page.keyboard.press("Space")
            else:
                await loc.evaluate(
                    "e => { const l = e.closest('label') || "
                    "(e.id ? document.querySelector('label[for=\"'+e.id+'\"]') : null); "
                    "if (l) l.click(); }")
            await page.wait_for_timeout(300)
            if await _checked():
                return True
        except Exception:
            continue
    return await _checked()


async def _click_popup_option(page, text):
    """Click a visible popup option whose text matches `text` (accent-insensitive)."""
    sels = ['[data-automation-id="promptOption"]', '[data-automation-id="promptLeafNode"]',
            '[role="option"]', '[role="listbox"] li', 'li[role="option"]']
    want = _deaccent(text)
    for sel in sels:
        loc = page.locator(sel)
        try:
            for i in range(min(await loc.count(), 60)):
                o = loc.nth(i)
                if not await o.is_visible():
                    continue
                t = _deaccent(((await o.inner_text()) or "").replace("\n", " "))
                # exact, or want-contains-option only for a substantive option
                # text (>=4 chars) so a stray 'i'/'in' can't match 'india (+91)'.
                if t == want or want in t or (len(t) >= 4 and t in want):
                    await o.click(timeout=3000)
                    return True
        except Exception:
            continue
    return False


async def _type_into_search(page, term):
    """Type `term` into a popup's search input so a long typeahead filters.
    Uses REAL keystrokes (keyboard.type) — NOT .fill() — because Workday's
    React filter only updates on key events, not on a programmatic value set
    (that's why 'India' appeared in the box but the list stayed unfiltered).
    Returns True if a search input was found and typed into."""
    sels = ['[data-automation-id="searchBox"]', '[data-automation-id="textInputBox"]',
            'input[aria-autocomplete]', '[role="combobox"] input', '[role="textbox"]',
            'input[placeholder*="earch" i]', '[role="listbox"] input',
            '[data-automation-id*="search" i] input', 'input[type="text"]']
    for s in sels:
        loc = page.locator(s)
        try:
            for i in range(await loc.count()):
                el = loc.nth(i)
                if not await el.is_visible():
                    continue
                await el.click(timeout=1500)
                # Clear anything already typed, then type with real keystrokes.
                try:
                    await el.press("Control+A"); await el.press("Backspace")
                except Exception:
                    pass
                await el.type(term, delay=80)        # fires React onChange/onKeyUp
                return True
        except Exception:
            continue
    # Fallback: the popup may have auto-focused its search box on open — type
    # into whatever is focused, with real keystrokes.
    try:
        await page.keyboard.press("Control+A"); await page.keyboard.press("Backspace")
        await page.keyboard.type(term, delay=80)
        return True
    except Exception:
        return False


async def _field_shows_value(page, idx):
    """Does the field now display a non-placeholder value? (apply verification)"""
    try:
        t = await page.locator(f'[data-agent-idx="{idx}"]').first.evaluate(
            """e => {
                // Native <select>: read the SELECTED option only — its innerText
                // is the whole option list, which would always look 'filled'.
                if (e.tagName === 'SELECT') {
                    const o = e.selectedOptions && e.selectedOptions[0];
                    return (o ? (o.text || o.value) : (e.value || '')).trim();
                }
                return (e.value || e.innerText || e.textContent || '').trim();
            }""")
    except Exception:
        return False
    return bool((t or "").strip()) and not _is_placeholder_value(t)


async def _field_display_text(page, idx):
    """The text a prompt/select shows while CLOSED (its selection chip / display
    value), or ''. Robust fallback to _selected_chip that does NOT depend on
    chip automation-ids: a closed Workday prompt renders no option list, so the
    container's own text is just the selected value(s) (+ maybe the label).
    Returns '' if an option popup is currently open (then the text is unreliable).
    Compare a target value against this ONLY before opening the dropdown."""
    try:
        return await page.locator(f'[data-agent-idx="{idx}"]').first.evaluate(
            """el => {
                const root = el.closest('[data-automation-id*="multiselect" i], '
                  + '[data-automation-id*="selectWidget" i], [class*="multiSelect"], '
                  + '[class*="selectWidget"], [data-automation-id]')
                  || el.parentElement || el;
                // If an option list is open inside us, the text is the menu, not
                // a selection — bail so we don't misread it.
                if (root.querySelector('[role=listbox], [role=option], '
                      + '[data-automation-id*="promptOption"]')) return '';
                const t = ((el.value || '') || root.innerText || root.textContent || '')
                            .replace(/\\s+/g, ' ').trim();
                const low = t.toLowerCase();
                if (!t || low === 'select one' || low === 'search') return '';
                return t.slice(0, 120);
            }""") or ""
    except Exception:
        return ""


async def _selected_chip(page, idx):
    """For a typeahead/multiselect, return the already-selected value text (the
    chip), or '' if nothing is selected. Reads the SELECTED-ITEM element's own
    text (the same chip selectors collect_elements uses) — NOT the container's
    innerText — so it works whether the popup is open or closed and never picks
    up the 'Search' box or the option list. This stops the engine from
    re-opening + re-selecting an already-filled dropdown (the India(+91) toggle)."""
    try:
        return await page.locator(f'[data-agent-idx="{idx}"]').first.evaluate(
            """el => {
                // The chip lives in the field container, which may be the input's
                // ancestor OR a sibling subtree. Walk up a few levels and search.
                let cursor = el;
                const SEL = '[data-automation-id*="selectedItem"], '
                  + '[data-automation-id*="selectedValue"], '
                  + '[data-automation-id*="selectedListItem"], '
                  + '[class*="selectedItem"], [class*="multiSelectChip"], '
                  + '[class*="selectedChip"]';
                for (let h = 0; h < 5 && cursor; h++) {
                    const chips = cursor.querySelectorAll(SEL);
                    for (const ch of chips) {
                        const t = (ch.innerText || ch.textContent || '')
                            .replace(/\\s+/g, ' ')
                            .replace(/^[×xX✕✖]\\s*/, '').replace(/\\s*[×xX✕✖]$/, '').trim();
                        if (!t) continue;
                        const low = t.toLowerCase();
                        if (low === 'select one' || low.startsWith('search')) continue;
                        if (t.length <= 80) return t;
                    }
                    cursor = cursor.parentElement;
                }
                return '';
            }""") or ""
    except Exception:
        return ""


async def _remove_chip(page, idx):
    """Click the × / remove button on a selected chip in this field (so a wrong
    leftover value can be replaced cleanly). Returns True if one was removed."""
    try:
        removed = await page.locator(f'[data-agent-idx="{idx}"]').first.evaluate(
            """el => {
                let cursor = el;
                for (let h = 0; h < 5 && cursor; h++) {
                    const b = cursor.querySelector(
                        'button[aria-label*="Delete" i], button[aria-label*="Remove" i], '
                      + '[data-automation-id*="DELETE" i], [data-automation-id*="dismiss" i]');
                    if (b) { b.click(); return true; }
                    cursor = cursor.parentElement;
                }
                return false;
            }""")
        if removed:
            await page.wait_for_timeout(300)
        return bool(removed)
    except Exception:
        return False


async def _fill_chiplist(page, idx, items):
    """Multi-value typeahead (Skills): type each term, pick the popup chip.
    Best-effort; skills are usually optional so failures aren't fatal."""
    sel = f'[data-agent-idx="{idx}"]'
    added = 0
    for it in [str(x).strip() for x in items if str(x).strip()][:15]:
        try:
            loc = page.locator(sel).first
            await loc.scroll_into_view_if_needed(timeout=2000)
            await loc.click(timeout=3000, force=True)
            try:
                await page.keyboard.press("Control+A"); await page.keyboard.press("Backspace")
            except Exception:
                pass
            await page.keyboard.type(it, delay=45)
            await page.wait_for_timeout(750)
            opts = await page.evaluate(_OPT_JS) or []
            m = _strong_match(it, opts)
            if m and await _click_popup_option(page, m):
                added += 1
            else:
                # Some chip inputs accept the typed term on Enter.
                try:
                    await page.keyboard.press("Enter"); await page.wait_for_timeout(250)
                    added += 1
                except Exception:
                    pass
            try: await page.keyboard.press("Escape")
            except Exception: pass
            await page.wait_for_timeout(150)
        except Exception:
            continue
    return added > 0, f"{added} added"


# ── error scan ───────────────────────────────────────────────────────────────
_ERR_JS = r"""
() => {
    const out = [];
    const inlineSels = ['[data-automation-id="errorMessage"]','[role="alert"]',
        '.errorMessage','.field-error','[class*="errorMessage"]','[class*="error-message"]'];
    document.querySelectorAll(inlineSels.join(', ')).forEach(e => {
        const txt=(e.innerText||'').replace(/\s+/g,' ').trim();
        if(!txt||txt.length<5) return;
        let cur=e, fe=null;
        for(let i=0;i<6&&cur;i++){cur=cur.parentElement;if(!cur)break;
            fe=cur.querySelector('input,select,textarea,button,[role="combobox"],[contenteditable]');if(fe)break;}
        const idx=fe?fe.getAttribute('data-agent-idx'):null;
        let label=fe?(fe.getAttribute('aria-label')||''):'';
        out.push({idx:idx?parseInt(idx):null,label:(label||'').trim().slice(0,80),error:txt.slice(0,160)});
    });
    const re=/^(Error|Alert)\s*[-:–—]\s*(.+)$/i;
    document.querySelectorAll('a,button,li,span,div').forEach(e=>{
        const r=e.getBoundingClientRect(); if(r.width<5||r.height<5) return;
        const txt=(e.innerText||'').replace(/\s+/g,' ').trim();
        if(!txt||txt.length>120) return;
        const m=re.exec(txt); if(!m) return;
        out.push({idx:null,label:m[2].slice(0,80),error:m[0].slice(0,160),kind:m[1].toLowerCase()});
    });
    return out;
}
"""
_SUCCESS = ("successfully", "upload complete", "uploaded", "complete!",
            "saved", "thank you", "is loaded")
# Transient server-side errors (NOT a fillable field). Their text carries a
# random uuid that changes every attempt, which otherwise defeats the
# no-progress guard and makes the loop run all attempts.
_TRANSIENT = ("error code:", "vps|", "page error")

async def scan_page_errors(page):
    try:
        raw = await page.evaluate(_ERR_JS) or []
    except Exception as ex:
        print(f"  (error-scan failed: {ex})")
        return []
    errs, seen = [], set()
    for e in raw:
        et = (e.get("error") or "").lower()
        lab = (e.get("label") or "").strip().lower()
        if (e.get("kind") or "").lower() == "alert":
            continue
        if any(s in et for s in _SUCCESS) and "error" not in et:
            continue
        if "alert" in et and "error" not in et:
            continue
        if any(s in et or s in lab for s in _TRANSIENT):   # server glitch, skip
            continue
        if lab.startswith("delete ") and any(s in et for s in _SUCCESS):
            continue
        k = lab or et[:30]
        if k in seen:
            continue
        seen.add(k); errs.append(e)
    return errs


# ── advance ──────────────────────────────────────────────────────────────────
_GATEWAY_TEXTS = ["Apply Manually", "Apply manually", "Start Application",
                  "Start Your Application", "Apply Now", "Apply"]
_GATEWAY_BAD = ("autofill", "linkedin", "last application", "resume")

async def _step_sig(page):
    """A signal that a multi-step form moved to a new step even when the URL does
    NOT change (Workday is an SPA — every step shares the same /apply URL). Reads
    the active progress-bar step, else the main page heading. Returns '' when not
    determinable, so the caller cleanly falls back to URL-change detection only."""
    try:
        return ((await page.evaluate(
            "() => {"
            "  const a = document.querySelector("
            "    \"[data-automation-id='progressBarActiveStep'],[aria-current='step'],[aria-current='page']\");"
            "  if (a && a.innerText) return a.innerText.trim();"
            "  const h = document.querySelector("
            "    \"[data-automation-id='pageHeader'], h1, h2\");"
            "  return h && h.innerText ? h.innerText.trim() : '';"
            "}")) or "").strip()
    except Exception:
        return ""


async def advance(page):
    """Forward-button ladder: auth -> NEXT -> submit-guard -> text fallback."""
    # Auth-step buttons first: on Workday's Create Account / Sign In page the
    # page-footer "Save and Continue" button exists but is inert until the
    # account is created.  Checking auth buttons first ensures we click the
    # functional submit (createAccountSubmitButton / signInSubmitButton).
    # Workday's noCaptchaWrapper overlays a click_filter div on top of
    # auth buttons — try the overlay first, fall back to the button itself.
    cfd = page.locator("[data-automation-id='noCaptchaWrapper'] "
                       "[data-automation-id='click_filter']")
    abt = page.locator("[data-automation-id='signInSubmitButton'], "
                       "[data-automation-id='createAccountSubmitButton']")
    nbt = page.locator(WORKDAY_NEXT_BUTTON)
    sbt = page.locator(WORKDAY_SUBMIT_BUTTON)
    if await cfd.count() > 0:
        try:
            await cfd.first.scroll_into_view_if_needed(timeout=2000)
            await cfd.first.click(timeout=5000); return True, "AUTH click_filter"
        except Exception:
            pass
    if await abt.count() > 0 and await abt.first.is_visible():
        try:
            await abt.first.click(timeout=5000, force=True); return True, "AUTH submit"
        except Exception as ex:
            return False, f"AUTH err {ex}"
    if await nbt.count() > 0 and await nbt.first.is_visible():
        try:
            await nbt.first.click(timeout=5000, force=True); return True, "WORKDAY_NEXT"
        except Exception as ex:
            return False, f"NEXT err {ex}"
    if await sbt.count() > 0 and await sbt.first.is_visible():
        return False, "SUBMIT visible — final review, NOT auto-clicking"
    # NOTE: we deliberately do NOT click gateway buttons ("Apply", "Apply Now",
    # "Apply Manually") here. On a form page a stray "Apply" link would re-navigate
    # and loop. Gateway click-through (landing → application) is handled ONCE,
    # up front, by apply_engine.gateway_advance() in the orchestrator.
    # Generic forward-button text fallback (non-standard tenants whose button
    # lacks the stable WORKDAY_NEXT automation-id).
    for t in ("Save and Continue", "Save & Continue", "Continue", "Next", "Save"):
        loc = page.locator(f'button:has-text("{t}"), [role="button"]:has-text("{t}")')
        for i in range(await loc.count()):
            b = loc.nth(i)
            try:
                if not await b.is_visible():
                    continue
                bt = (await b.inner_text() or "").strip().lower()
            except Exception:
                continue
            # avoid 'Save for later' / 'Save draft' style buttons
            if "later" in bt or "draft" in bt:
                continue
            try:
                await b.scroll_into_view_if_needed(timeout=2000)
                await b.click(timeout=5000, force=True)
                return True, f"text-advance {t!r}"
            except Exception:
                continue
    return False, "no forward button found"


# ── phantom-row delete ───────────────────────────────────────────────────────
async def _delete_phantom_rows(page, profile):
    """Delete rows the page shows beyond what the profile has (one per call)."""
    elements, idx_frame = await collect_elements(page)
    for e in elements:
        if (e.get("label") or "").strip().lower() not in ("delete", "remove"):
            continue
        p = _parse_sec(e.get("section_label"))
        if not p:
            continue
        key, i0 = p
        if i0 >= len(profile.get(key) or []):
            ok, _ = await execute_action(page, {"action": "click", "index": e["idx"],
                     "label": "Delete"}, idx_frame, elements, "", {})
            await settle(page)
            return True
    return False


# ── per-element fill (classify -> resolve -> apply) ──────────────────────────
def _is_password_field(e):
    typ = (e.get("type") or "").lower()
    lab = (e.get("label") or "").lower()
    return typ == "password" or "password" in lab

async def _fill_password(page, e, idx_frame, elements, creds):
    """Fill a password / verify-password field from .env creds (same value for
    both, so Workday's 'passwords do not match' validator passes). Blur after."""
    idx, label = e.get("idx"), e.get("label", "")
    pw = (creds or {}).get("password") or ""
    if not pw:
        return "nodata", f"[{idx}] {label}: no password in .env (set APPLY_PASSWORD)"
    ok, _ = await execute_action(page, {"action": "fill", "index": idx,
             "value": pw, "label": label}, idx_frame, elements, "", creds)
    try:  # force blur so the match-validator runs
        await page.locator(f'[data-agent-idx="{idx}"]').first.press("Tab")
    except Exception:
        pass
    return ("filled", f"[{idx}] {label}=***") if ok else ("fail", f"[{idx}] {label}")


def _looks_like_question_label(label):
    l = (label or "").strip()
    return l.endswith("?") or len(l.split()) > 6


def _clean_opts(raw):
    out = []
    for o in (raw or []):
        o = (o or "").strip()
        if o and not _is_placeholder_value(o) and o not in out:
            out.append(o)
    return out

def _related(target, shown):
    """Does the committed display value `shown` correspond to `target`?"""
    if not shown:
        return False
    g, t = _deaccent(shown), _deaccent(target)
    if t in g or g in t:
        return True
    return any(_deaccent(w) in g for w in re.findall(r"[A-Za-z]{3,}", str(target)))

async def _select_once(page, idx, term):
    """Distilled Workday typeahead selection: open -> clear -> type -> debounce
    -> Enter (snaps to the match) -> chip-aware verify. ONE attempt (~2.5s) with
    a RELIABLE verify (reads the sibling chip), so we never burn base.py's whole
    4-term ladder. Returns the value the field now shows (chip/display) or ''."""
    sel = f'[data-agent-idx="{idx}"]'
    try:
        loc = page.locator(sel).first
        await loc.scroll_into_view_if_needed(timeout=2000)
        await loc.click(timeout=3000, force=True)
        await page.wait_for_timeout(550)
        try:
            await page.keyboard.press("Control+A"); await page.keyboard.press("Backspace")
        except Exception:
            pass
        await page.keyboard.type(str(term), delay=55)
        await page.wait_for_timeout(550)            # debounce before Enter snaps
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(650)
    except Exception:
        return ""
    chip = await _selected_chip(page, idx)
    if chip:
        return chip
    # React-select: check for a committed single-value element, NOT the input's
    # typed text. After typing "years" + Enter on a non-searchable dropdown,
    # the input still shows "years" but no option was actually selected —
    # _field_shows_value reads e.value and falsely reports success.
    try:
        sv = await page.locator(f'[data-agent-idx="{idx}"]').first.evaluate(
            """el => {
                const c = el.closest('.select__control, .select-shell, '
                  + '.select__value-container, [class*="singleValue"], '
                  + '[class*="select-container"], [class*="selectContainer"]')
                  || el.parentElement || el;
                const sv = c.querySelector('[class*="single-value"], [class*="singleValue"]');
                return sv ? sv.innerText.trim() : '';
            }""")
        if sv:
            return sv
    except Exception:
        pass
    # For native inputs / non-react-select: fall back to the original check,
    # but only if the field is NOT a react-select (input inside .select__control).
    try:
        is_react = await page.locator(f'[data-agent-idx="{idx}"]').first.evaluate(
            "el => !!el.closest('[class*=\"select__\"], [class*=\"react-select\"]')")
    except Exception:
        is_react = False
    if not is_react and await _field_shows_value(page, idx):
        return await _field_display_text(page, idx) or str(term)
    return ""

async def _open_and_click_option(page, idx, target):
    """Commit a NON-searchable react-select (e.g. options '0-2','3-8','9+'):
    open the control, then CLICK the option whose text matches `target` using
    Playwright's has_text filter (fast + precise — iterating raw [role=option]
    hit 247 hidden phone-country items and timed the dropdown out). Verifies via
    the control's own single-value. Returns the value shown, or ''."""
    sel = f'[data-agent-idx="{idx}"]'
    try:
        loc = page.locator(sel).first
        await loc.scroll_into_view_if_needed(timeout=2000)
        await loc.click(timeout=3000, force=True)            # open
        try:  # clear any stale typeahead filter from a prior attempt
            await page.keyboard.press("Control+A"); await page.keyboard.press("Backspace")
        except Exception:
            pass
        await page.wait_for_timeout(450)
    except Exception:
        return ""
    clicked_opt = None
    tfold = _deaccent(str(target))
    for osel in (".select__option", "[class*='select__option']",
                 "[role='option']", "li[role='option']",
                 # Phone-country widgets (intl-tel-input, react-international-phone)
                 # render options as plain <li> with NO role=option and no 'option'
                 # in the class — match their library markup, then any visible <li>
                 # in a freshly-opened list as a catch-all (this is what was missing
                 # for Greenhouse's phone country selector).
                 "li.iti__country", ".iti__country-list li", ".country-list li",
                 "[class*='country-selector'] li", "[class*='country'] li",
                 "[class*='dropdown'] li", "[class*='menu'] li",
                 "li[role='menuitem']", "li"):
        try:
            cand = page.locator(osel).filter(has_text=str(target))
            cnt = await cand.count()
            if cnt == 0:
                continue
            # EXACT match first: substring 'India' also matches 'British Indian
            # Ocean Territory' (+246) — picking .first would select the wrong
            # country. Scan candidates and prefer an exact (deaccented) text match;
            # only fall back to the first substring hit if no exact match exists.
            exact = None
            first_visible = None
            for i in range(min(cnt, 60)):
                o = cand.nth(i)
                try:
                    if not await o.is_visible():
                        continue
                    txt = _deaccent((await o.inner_text()) or "")
                except Exception:
                    continue
                if first_visible is None:
                    first_visible = o
                base = txt.split("+")[0].strip()   # drop trailing dial code
                if txt == tfold or base == tfold or txt.startswith(tfold + " "):
                    exact = o
                    break
            opt = exact or first_visible
            if opt is not None:
                await opt.scroll_into_view_if_needed(timeout=1500)
                await opt.click(timeout=2000)
                await page.wait_for_timeout(500)
                clicked_opt = opt
                break
        except Exception:
            continue

    # Library-agnostic success signal: clicking an option closes the list, so if
    # the option we clicked is gone, the selection committed. Covers widgets (the
    # intl-tel-input phone country) whose selected value lives in a flag/title
    # attribute that the react-select single-value check below cannot read.
    if clicked_opt is not None:
        try:
            if not await clicked_opt.is_visible():
                return str(target)
        except Exception:
            return str(target)
    # Verify via the control's single-value, scoped to THIS field's control.
    # (Must use .select__control / .select-shell — NOT a generic [class*=
    # container], which matches the narrow .select__input-container that does
    # not hold the selected value.)
    try:
        shown = await loc.evaluate(
            "el => { const sh = el.closest('.select__control, .select-shell,"
            " .select__value-container') || document;"
            " const sv = sh.querySelector('[class*=\"single-value\"]');"
            " return sv ? sv.innerText.trim() : ''; }")
        if shown:
            return shown
    except Exception:
        pass
    chip = await _selected_chip(page, idx)
    if chip:
        return chip
    if await _field_shows_value(page, idx):
        return await _field_display_text(page, idx) or str(target)
    return ""


def _term_of(target):
    """The most discriminating word to type for a typeahead search."""
    ws = re.findall(r"[A-Za-z]{3,}", str(target))
    return ws[0] if ws else str(target)

async def _handle_select(page, e, val, idx_frame, elements, profile, user_id,
                         gemini_client, held_list, creds=None):
    """Fill a dropdown / typeahead. Resolution (cache -> direct value -> scrape +
    strong/LLM/decline/hold) lives here; the ACTUAL selection is delegated to the
    proven execute_action -> base.py click_option (type -> debounce -> Enter-snap
    -> verify). Returns (status, note). Skips if a matching chip is already set;
    removes a wrong leftover chip first. Handles country-code, State (accented),
    and semantic mismatches (MTech -> Master's) via cache/LLM."""
    creds = creds or {"email": "", "password": ""}
    idx, label = e.get("idx"), e.get("label", "")

    # Pre-check: a typeahead/multiselect that already shows a selected chip is
    # DONE — don't re-open and re-search it. This was the "India (+91) selected
    # but it keeps searching again and again" bug: the chip wasn't read as the
    # field value, so every pass re-typed and ended up selecting nothing.
    #
    # NATIVE <select> ONLY: skip this pre-check. A <select>'s innerText is the
    # WHOLE option list ("Please Select Dormitory Home ... Personal Mobile"), so
    # the substring test would spuriously match the target ('Personal Mobile' is
    # inside that string) and wrongly report "already set". Native selects have
    # their own idempotent branch below (re-selecting is harmless) + a
    # selectedOption-aware verify, so they don't need the chip pre-check.
    if e.get("tag") != "select":
        already = await _selected_chip(page, idx) or await _field_display_text(page, idx)
        if already and val:
            want, got = _deaccent(str(val)), _deaccent(already)
            words = re.findall(r"[A-Za-z]{3,}", str(val))
            if want in got or got in want or any(_deaccent(w) in got for w in words):
                return "skip", f"[{idx}] {label} already set ({already[:30]})"
            # A chip is present but it's the WRONG value (e.g. 'Afghanistan (+93)'
            # left over from an earlier mis-pick) — remove it so the new selection
            # replaces it cleanly instead of stacking / being ignored.
            if await _selected_chip(page, idx):
                await _remove_chip(page, idx)

    # Native <select>: base.py's select_option(label=...) is instant + exact —
    # no typing/Enter dance. Route it straight there.
    if e.get("tag") == "select":
        tgt = _cache_get(profile, label, val) or val or ""
        if tgt:
            ok, _ = await execute_action(page, {"action": "select", "index": idx,
                     "value": tgt, "label": label}, idx_frame, elements, "", creds)
            if ok or await _field_shows_value(page, idx):
                _cache_set(profile, user_id, label, val, tgt)
                return "filled", f"[{idx}] {label}={str(tgt)[:30]} [native]"
        # No/failed target → read the <option> list straight from the DOM (never
        # click: opening a native <select> shows an OS popup that freezes the
        # page) and resolve via cache/strong-match/LLM/auto-answer/hold.
        opts = []
        try:
            raw = await page.locator(f'[data-agent-idx="{idx}"] option').all_inner_texts()
            opts = [o.strip() for o in raw
                    if o.strip() and not o.strip().lower().startswith(
                        ("please select", "select one", "select an option", "choose", "--"))]
        except Exception:
            pass
        if opts:
            res = await _resolve_choice_core(idx, label, val, opts, profile, user_id,
                                             gemini_client, held_list)
            if res.held or not res.value:
                return "held", f"[{idx}] {label} [HELD: confirm dropdown]"
            ok, _ = await execute_action(page, {"action": "select", "index": idx,
                     "value": res.value, "label": label}, idx_frame, elements, "", creds)
            if ok or await _field_shows_value(page, idx):
                _cache_set(profile, user_id, label, val, res.value)
                return "filled", f"[{idx}] {label}={str(res.value)[:30]} [native+{res.source}]"
        return "fail", f"[{idx}] {label}={tgt!r} [native select failed]"

    # ── Universal dropdown strategy ──────────────────────────────────────
    # 1. Scrape options (open → read → close). Safe for ALL dropdown types.
    # 2. Resolve which option to pick (cache → strong match → LLM).
    # 3. Commit by CLICKING the option (safe, works everywhere).
    # 4. Fall back to typing only if clicking didn't work (long typeaheads).
    #
    # The old approach typed first, which corrupts any dropdown that filters
    # on keystrokes (react-select, custom selects, etc.) — "No options" kills
    # all subsequent attempts.

    opts = await _scrape_options(page, idx, type_hint=val)
    cached = _cache_get(profile, label, val)

    if opts:
        res = await _resolve_choice_core(idx, label, val, opts, profile, user_id,
                                         gemini_client, held_list)
        if res.held or not res.value:
            return "held", f"[{idx}] {label} [HELD: confirm dropdown]"
        target = res.value
        src = res.source
    elif cached:
        target, src = cached, "cached"
    elif val:
        target, src = val, "direct"
    else:
        return "nodata", f"[{idx}] {label}: no options and no profile value"

    # Attempt 1: click the option (safe — doesn't corrupt the dropdown).
    shown = await _open_and_click_option(page, idx, target)
    if shown and _related(target, shown):
        _cache_set(profile, user_id, label, val, shown)
        return "filled", f"[{idx}] {label}={shown[:30]} [{src}+click]"

    # Attempt 2: typeahead — type + Enter (for Workday-style long lists where
    # Enter snaps to the match without filtering).
    shown = await _select_once(page, idx, _term_of(target))
    if shown and _related(target, shown):
        _cache_set(profile, user_id, label, val, shown)
        return "filled", f"[{idx}] {label}={shown[:30]} [{src}]"

    # Attempt 3: base.py ladder (handles edge cases we haven't covered).
    await execute_action(page, {"action": "select", "index": idx,
             "value": target, "label": label}, idx_frame, elements, "", creds)
    chip = await _selected_chip(page, idx)
    if chip and target and not _related(target, chip):
        try:
            await _remove_chip(page, idx)
        except Exception:
            pass
        return "fail", f"[{idx}] {label}: wrong option {chip[:24]!r} (wanted {str(target)[:24]!r})"
    committed = bool(chip) or await _field_shows_value(page, idx)
    return (("filled", f"[{idx}] {label}={(chip or target)[:30]} [{src}+exec]")
            if committed else ("fail", f"[{idx}] {label}={target!r} (did not commit)"))


async def _click_country_option(page, country, opt_selectors):
    """Click the visible option whose name EXACTLY equals `country`. Substring
    matching 'India' also hits 'British Indian Ocean Territory' (+246), so we
    require an exact (case-insensitive) name match, ignoring a trailing dial code
    like 'India +91'. Reads the .iti__country-name span when present else the
    element's own text. Returns True on click."""
    cfold = country.casefold()
    for osel in opt_selectors:
        try:
            opts = page.locator(osel)
            cnt = await opts.count()
            if cnt == 0:
                continue
            for i in range(min(cnt, 300)):
                o = opts.nth(i)
                try:
                    if not await o.is_visible():
                        continue
                    nm = o.locator(".iti__country-name")
                    if await nm.count() > 0:
                        name = (await nm.first.inner_text() or "").strip()
                    else:
                        name = (await o.inner_text() or "").strip()
                except Exception:
                    continue
                nf = name.casefold()
                if nf == cfold or nf.startswith(cfold + " ") or nf.split("+")[0].strip() == cfold:
                    await o.scroll_into_view_if_needed(timeout=1500)
                    await o.click(timeout=2000)
                    await page.wait_for_timeout(400)
                    return True
        except Exception:
            continue
    return False


async def _fix_phone_country(page, profile, on_notify=None):
    """Deterministically set the phone-country selector to the candidate's country.

    Two widget families, tried in order:
      A. react-select COMBOBOX (Greenhouse/Jumio 'Country' box) — a text input that
         FILTERS the 240-country list as you type. Opening it and scrolling lands on
         a random country (Azerbaijan in testing); the reliable path is to TYPE the
         country name to filter, then click the exact match.
      B. intl-tel-input flag button + search box + <li> list.

    Returns the country name on success, else ''. Fully defensive — never throws."""
    country = str(profile.get("country") or "").strip() or "India"
    try:
        # ── Path A: react-select country combobox — TYPE to filter, then click ──
        for cb in ("input#country",
                   ".phone-input__country input.select__input",
                   ".phone-input__country input[role='combobox']",
                   "input[role='combobox'][aria-labelledby*='country' i]"):
            try:
                box = page.locator(cb).first
                if await box.count() == 0 or not await box.is_visible():
                    continue
                await box.scroll_into_view_if_needed(timeout=1500)
                await box.click(timeout=2000)
                # Real keystrokes (not fill) so react-select's filter actually fires.
                try:
                    await box.fill("", timeout=1000)
                except Exception:
                    pass
                await box.press_sequentially(country, delay=40)
                await page.wait_for_timeout(600)
                if await _click_country_option(
                        page, country,
                        (".select__option", "[class*='select__option']",
                         "[role='option']", "[id*='option']")):
                    if on_notify:
                        await on_notify(f"☎️ Phone country set to {country}")
                    return country
            except Exception:
                continue

        # ── Path B: intl-tel-input flag button + search ──
        opener = None
        for s in (".iti__selected-country", ".iti__selected-flag",
                  "[class*='country-selector'] button",
                  "[aria-label='Select country']",
                  "[aria-label*='phone country' i]", "[aria-label*='country code' i]"):
            loc = page.locator(s).first
            if await loc.count() > 0 and await loc.is_visible():
                opener = loc
                break
        if opener is not None:
            await opener.scroll_into_view_if_needed(timeout=1500)
            await opener.click(timeout=3000)
            await page.wait_for_timeout(400)
            for ss in (".iti__search-input", "input[type='search']",
                       "[class*='search'] input"):
                try:
                    si = page.locator(ss).first
                    if await si.count() > 0 and await si.is_visible():
                        await si.fill(country, timeout=1500)
                        await page.wait_for_timeout(400)
                        break
                except Exception:
                    continue
            if await _click_country_option(
                    page, country,
                    ("li.iti__country", ".iti__country-list li", "[role='option']")):
                if on_notify:
                    await on_notify(f"☎️ Phone country set to {country}")
                return country
        return ""
    except Exception:
        return ""


async def _fill_one(page, e, idx_frame, elements, profile, user_id, creds,
                    resume_path, upload_state, gemini_client, held_list,
                    handled_groups=None, force=False, fill_memo=None):
    """classify -> resolve -> apply for ONE element. Returns (status, note),
    status in {'filled','nodata','fail','skip','held'}.
    force=True (error-correction): ignore the already-filled guard and clear
    text fields before refilling.
    fill_memo: per-RUN set of dropdown/radio/chiplist fields already filled
    successfully — skip them on later passes so we never re-open (the toggle)."""
    idx   = e.get("idx")
    label = e.get("label", "")
    sl    = e.get("section_label") or ""
    itype = classify_field(e)

    if itype == "skip":
        return "skip", ""

    # Career-site chat widgets (Phenom "Career Bot" etc.) get collected like
    # form fields — never type into them: auto-draft once MESSAGED a recruiter
    # chatbot instead of applying.
    _ll = (label or "").lower()
    if "chatbot" in _ll or "chat bot" in _ll or "career bot" in _ll or \
       ("chat" in _ll and ("input" in _ll or "message" in _ll or "send" in _ll)):
        return "skip", f"[{idx}] {label[:40]} (chat widget)"
    # Site search boxes are never application fields (live test typed the
    # candidate's name into a careers-site search bar).
    if _ll.strip() in ("search", "search jobs", "search job", "keyword", "keywords",
                       "search by keyword", "job search"):
        return "skip", f"[{idx}] {label[:40]} (site search)"

    # ---- password / verify-password (from .env, BEFORE filled-guard since the
    #      field may already show masked dots) ----
    if _is_password_field(e):
        return await _fill_password(page, e, idx_frame, elements, creds)

    # ---- per-run memo guard: a re-open-sensitive widget (select/typeahead/
    #      radio/chiplist) we already filled this run is DONE. This is the
    #      DOM-independent stop for the "fill India(+91) -> reopen -> refill"
    #      toggle. force=True (error correction) deliberately bypasses it. ----
    if (not force and fill_memo is not None and itype in _MEMO_TYPES
            and _memo_key(e, itype) in fill_memo):
        return "skip", ""

    # Already-filled guard: if a field shows a real (non-placeholder) value,
    # trust it and move on. The vision audit catches genuinely wrong values
    # (e.g. wrong country) — string-comparing the profile value against the
    # displayed option is unreliable (profile "5 years 7 months" vs option
    # "3-8", profile "India" vs display "+91").
    if (not force and e.get("value")
            and not _is_placeholder_value(e.get("value"))
            and itype not in ("current_checkbox", "consent")):
        return "skip", ""

    # ---- upload ----
    if itype == "upload":
        if not resume_path:
            return "nodata", f"[{idx}] {label}: no resume file"
        if upload_state["done"]:
            return "skip", ""
        try:
            if await page.evaluate(
                "() => (document.body.innerText.toLowerCase()"
                ".match(/successfully uploaded/g)||[]).length") or 0:
                upload_state["done"] = True
                return "skip", ""
        except Exception:
            pass
        ok, _ = await execute_action(page, {"action": "upload", "index": idx,
                 "label": label}, idx_frame, elements, resume_path, creds)
        if ok:
            upload_state["done"] = True
        return ("filled", f"[{idx}] {label} [upload]") if ok else ("fail", f"[{idx}] {label} [upload]")

    # ---- currently-work-here checkbox ----
    if itype == "current_checkbox":
        want = _row_is_current(sl, profile)
        if want and not e.get("checked"):
            ok = await _toggle_check(page, idx)
            return ("filled", f"[{idx}] {label} [check is_current]") if ok else ("fail", f"[{idx}] {label}")
        return "skip", ""

    # ---- consent / acknowledgment checkbox: tick it ----
    if itype == "consent":
        if e.get("checked"):
            return "skip", ""
        ok = await _toggle_check(page, idx)
        return ("filled", f"[{idx}] {label} [consent ✓]") if ok else ("fail", f"[{idx}] {label}")

    # ---- a REQUIRED bare checkbox is almost always an acknowledgment box
    #      (consent text often lives in a sibling, so no keyword in the label).
    #      Tick it. "I currently work here" is handled above and isn't required.
    if itype == "checkbox_other":
        if e.get("required") and not e.get("checked"):
            ok = await _toggle_check(page, idx)
            return ("filled", f"[{idx}] {label} [required checkbox ✓]") if ok else ("fail", f"[{idx}] {label}")
        return "skip", ""

    # ---- radio group: resolve the question, click the matching option ----
    if itype == "radio":
        group_q = (e.get("q") or label or "").strip()
        if handled_groups is not None and group_q in handled_groups:
            return "skip", ""
        group = [x for x in elements
                 if (x.get("control") == "radio" or x.get("type") == "radio")
                 and ((x.get("q") or x.get("label") or "").strip() == group_q)]
        options = [x.get("option") for x in group if x.get("option")]
        if handled_groups is not None:
            handled_groups.add(group_q)
        if not options:
            return "nodata", f"[{idx}] {group_q[:50]} [radio: no options]"
        pv = resolve_field_value({"label": group_q, "section_label": sl}, profile)
        res = await _resolve_choice_core(idx, group_q, pv, options, profile,
                                         user_id, gemini_client, held_list)
        if res.held:
            return "held", f"[{idx}] {group_q[:50]} [radio HELD]"
        if not res.value:
            return "nodata", f"[{idx}] {group_q[:50]} [radio: no answer]"
        target = next((x for x in group
                       if _strong_match(res.value, [x.get("option") or ""])), None)
        if not target:
            return "fail", f"[{idx}] {group_q[:40]}: no radio matches {res.value!r}"
        ok = await _toggle_check(page, target.get("idx"))
        ti = target.get("idx")
        return (("filled", f"[{ti}] {group_q[:35]}={res.value} [{res.source}]") if ok
                else ("fail", f"[{idx}] {group_q[:40]}={res.value}"))

    # ---- Skills / multi-value typeahead chip-list ----
    if itype == "chiplist":
        items = profile.get("skills") or []
        if not items:
            return "skip", f"[{idx}] {label} [chiplist: no skills in profile]"
        ok, note = await _fill_chiplist(page, idx, items)
        return ("filled", f"[{idx}] {label} [{note}]") if ok else ("fail", f"[{idx}] {label} [{note}]")

    # ---- date ----
    if itype == "date":
        val = resolve_field_value(e, profile)
        if not val:
            return "nodata", f"[{idx}] {label}" + (f" ({sl})" if sl else "") + " (current row -> blank)"
        ok = await _fill_date(page, idx, val)
        return ("filled", f"[{idx}] {label}={val} [date]") if ok else ("fail", f"[{idx}] {label}={val} [date]")

    # ---- select / typeahead ----
    if itype == "select":
        val = resolve_field_value(e, profile)
        if not val:
            # No profile value: don't silently skip. Resolve a choice from the
            # field's OWN options (native <select> options come baked in via
            # collect_elements; typeaheads get scraped), then LLM-choose / hold
            # via the same core radios use. Only give up if there are no options.
            opts = _clean_opts(e.get("options")) or await _scrape_options(page, idx)
            if opts:
                res = await _resolve_choice_core(idx, label, None, opts, profile,
                                                 user_id, gemini_client, held_list)
                if res.held:
                    return "held", f"[{idx}] {label} [HELD: confirm dropdown]"
                val = res.value
            if not val:
                return "nodata", f"[{idx}] {label}" + (f" ({sl})" if sl else "") + " (no profile value)"
        return await _handle_select(page, e, val, idx_frame, elements,
                                    profile, user_id, gemini_client, held_list)

    # ---- text / textarea ----
    val = resolve_field_value(e, profile)
    if not val:
        # A held free-text answer the user locked in earlier? Use it.
        cached = _cache_get(profile, label, "")
        if cached:
            val = cached
        elif _looks_like_question_label(label) or e.get("tag") == "textarea":
            # Free-text screening question with no profile data: LLM DRAFTS.
            draft = await _llm_draft(label, profile, gemini_client)
            # Unattended auto-answer: fill the draft instead of holding for review.
            if AUTO_ANSWER and draft:
                if force:
                    await _clear_text(page, idx)
                ok, _ = await execute_action(page, {"action": "fill", "index": idx,
                         "value": draft, "label": label}, idx_frame, elements, "", creds)
                return (("filled", f"[{idx}] {label[:40]} [auto-draft]") if ok
                        else ("fail", f"[{idx}] {label[:40]} [auto-draft]"))
            # Attended: hold for the user to approve (never auto-submit prose).
            print(f"\n  [CONFIRM NEEDED] free-text [{idx}] {label[:60]!r}")
            if draft:
                print(f"     LLM draft : {draft[:240]}")
            print(f"     -> approve/edit in a NEW cell, then re-run:")
            print(f"        resolve_field(profile, {label!r}, '', \"<your answer>\"); save_profile({user_id}, profile)")
            held_list.append({"idx": idx, "label": label, "value": "",
                              "suggestion": draft or "<your answer>", "kind": "free-text"})
            return "held", f"[{idx}] {label[:50]} [free-text HELD]"
        else:
            return "nodata", f"[{idx}] {label}" + (f" ({sl})" if sl else "") + " (no profile value)"
    # Numeric inputs (type=number, or "... in LPA / lakhs / days / number" labels)
    # reject units — "40 lpa" fails on a number field. Send just the number.
    if (e.get("type") == "number"
            or re.search(r"\b(lpa|lakhs?|in days|number|amount|years?|months?)\b", label.lower())):
        m = re.search(r"\d[\d,\.]*", str(val))
        if m:
            val = m.group(0).replace(",", "")
    if force:
        await _clear_text(page, idx)
    ok, _ = await execute_action(page, {"action": "fill", "index": idx,
             "value": val, "label": label}, idx_frame, elements, "", creds)
    return ("filled", f"[{idx}] {label}={str(val)[:30]}") if ok else ("fail", f"[{idx}] {label}")


async def _fill_pass(page, profile, user_id, creds, resume_path, upload_state,
                     gemini_client, held_list, fill_memo=None):
    elements, idx_frame = await collect_elements(page)
    if len(elements) < 3:
        # React boards (Glean's Greenhouse) hydrate the form AFTER load — an
        # early observe sees a near-empty page and converge spirals into
        # vision recovery. Wait once and re-observe before believing it.
        await page.wait_for_timeout(2500)
        elements, idx_frame = await collect_elements(page)
    _assign_section_rows(elements)   # number un-numbered repeated rows (HPE, etc.)
    buckets = {"filled": [], "nodata": [], "fail": [], "held": []}
    handled_groups = set()    # radio groups answered once per pass
    for e in elements:
        status, note = await _fill_one(page, e, idx_frame, elements, profile, user_id,
                                       creds, resume_path, upload_state, gemini_client,
                                       held_list, handled_groups=handled_groups,
                                       fill_memo=fill_memo)
        if status in buckets and note:
            buckets[status].append(note)
        # Remember a successfully-filled dropdown/radio/chiplist so later passes
        # don't re-open it (DOM-independent fix for the re-select toggle).
        if status == "filled" and fill_memo is not None:
            it = classify_field(e)
            if it in _MEMO_TYPES:
                fill_memo.add(_memo_key(e, it))
        await page.wait_for_timeout(120)
    return buckets


# ── error-targeted correction ────────────────────────────────────────────────
def _norm_lbl(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("*", "").strip())

# Widget types that must not be re-opened once filled this run (the toggle bug).
_MEMO_TYPES = ("select", "radio", "chiplist")
def _memo_key(e, itype):
    """Stable per-run identity for a filled field (idx is reassigned each
    collect, so key on type + normalized question/label + section instead)."""
    return (itype, _norm_lbl(e.get("q") or e.get("label")),
            e.get("section_label") or "")

_CONSENT_ERR_RE = re.compile(
    r"check the box|must agree|please agree|please accept|to continue|"
    r"required to (continue|proceed)|accept the|agree to the", re.I)

async def _correct_errored_fields(page, profile, user_id, creds, gemini_client,
                                  held_list, fill_memo=None):
    elements, idx_frame = await collect_elements(page)
    _assign_section_rows(elements)   # number un-numbered repeated rows (HPE, etc.)
    errors = await scan_page_errors(page)
    err_labels = [_norm_lbl(e.get("label")) for e in errors if _norm_lbl(e.get("label"))]
    # Label-less errors: some portals (e.g. HPE) render "This field is required"
    # in a sibling with no label, the field name living only in the error TEXT.
    # Keep those full texts as secondary haystacks so we can still locate the
    # field by substring — gated below to specific labels to avoid false hits.
    err_texts = [_norm_lbl(e.get("error")) for e in errors
                 if not _norm_lbl(e.get("label")) and _norm_lbl(e.get("error"))]

    # Consent-error fallback: errors like "Please check the box to continue"
    # name no field — tick every unchecked consent / required checkbox.
    consent_err = any(_CONSENT_ERR_RE.search((e.get("error") or "") + " " + (e.get("label") or ""))
                      for e in errors)
    fixed_consent = 0
    if consent_err:
        for el in elements:
            it = classify_field(el)
            if it in ("consent", "checkbox_other", "current_checkbox"):
                # don't tick "currently work here" via this fallback
                if it == "current_checkbox":
                    continue
                if not el.get("checked"):
                    if await _toggle_check(page, el.get("idx")):
                        print(f"    FIXED [{el.get('idx')}] {el.get('label','')[:50]} [consent ✓]")
                        fixed_consent += 1
        if fixed_consent:
            return fixed_consent

    targets, seen = [], set()
    for el in elements:
        if classify_field(el) == "skip":
            continue
        lab = _norm_lbl(el.get("label"))
        if not lab:
            continue
        matched = any(lab == nl or (len(lab) >= 3 and (lab in nl or nl in lab))
                      for nl in err_labels)
        # Fallback: field name embedded inside a label-less error's text. Require
        # a specific label (multi-word or >=5 chars) so short/common labels like
        # 'city' don't match an unrelated long error.
        if not matched and (len(lab) >= 5 or " " in lab):
            matched = any(lab in et for et in err_texts)
        if matched and el.get("idx") not in seen:
            seen.add(el.get("idx")); targets.append(el)
    if not targets:
        print("  (no errored field located to correct)")
        return 0
    # Reuse the SAME resolve+apply path as the fill pass, with force=True so
    # already-filled-but-wrong values get cleared and refilled. This makes every
    # field type (text/select/radio/date/consent/password/free-text) self-heal.
    fixed = 0
    handled_groups = set()
    upload_state = {"done": True}   # never re-upload during correction
    for e in targets:
        status, note = await _fill_one(page, e, idx_frame, elements, profile, user_id,
                                       creds, "", upload_state, gemini_client, held_list,
                                       handled_groups=handled_groups, force=True,
                                       fill_memo=fill_memo)
        ok = status == "filled"
        print(f"    {'FIXED' if ok else 'skip '} {note or e.get('label','')}")
        if ok:
            fixed += 1
            if fill_memo is not None:
                it = classify_field(e)
                if it in _MEMO_TYPES:
                    fill_memo.add(_memo_key(e, it))
        await page.wait_for_timeout(120)
    return fixed


def _print_held_summary(held_list, user_id):
    """One clear block listing every held field with a ready-to-paste line.
    (There is no input box — held fields are resolved by pasting these.)"""
    if not held_list:
        return
    print("\n" + "=" * 72)
    print(f"  HELD — {len(held_list)} field(s) need you. Paste into a NEW cell,")
    print("  edit the answer in quotes, run it, then re-run the converge cell:")
    print("=" * 72)
    for h in held_list:
        lab = h.get("label", ""); val = h.get("value", ""); sugg = h.get("suggestion", "")
        print(f"  resolve_field(profile, {lab!r}, {val!r}, {sugg!r})")
    print(f"  save_profile({user_id}, profile)")
    print("=" * 72)


# ── main convergence loop ────────────────────────────────────────────────────
def _site_domain(url: str) -> str:
    """Registrable domain (last two host labels) — for the converge origin
    guard. 'job-boards.greenhouse.io' → 'greenhouse.io'."""
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
        return ".".join(host.split(".")[-2:]) if host else ""
    except Exception:
        return ""


def _label_of_note(note):
    """Extract the field label from a fill-bucket note such as
    '[13] Phone Country [native select failed]' or '[5] Email=foo' → 'Phone
    Country' / 'Email'. Returns '' if not parseable. Used to target field-level
    grounded repair at the fields that actually failed."""
    s = re.sub(r"^\[\d+\]\s*", "", str(note or ""))   # drop leading [idx]
    s = s.split("=")[0]                                  # drop '=value'
    s = re.sub(r"\s*\[.*$", "", s)                       # drop trailing ' [note]'
    return s.strip()


async def converge_page(page, ctx, profile=None, user_id=1, *, gemini_client=None,
                        max_attempts=6, creds=None, on_notify=None, on_screenshot=None):
    """Fill the current page and advance. Returns a result dict (incl. final `page`).

    on_notify/on_screenshot (optional) stream live progress to the UI: each filled
    field is announced and a frame is pushed AFTER each fill pass. These fire at
    sequential points only (never concurrently with a browser action), so they add
    visibility without introducing Playwright races."""
    if profile is None:
        profile = load_profile(user_id)
    # One-time, idempotent: re-key legacy dropdown caches under the stable stem
    # so semantic mappings (e.g. MTech -> Master's) hit on an EMPTY dropdown.
    if _migrate_cache(profile):
        try: save_profile(user_id, profile)
        except Exception: pass
    if creds is None:
        creds = {"email": os.getenv("APPLY_EMAIL", ""), "password": os.getenv("APPLY_PASSWORD", "")}
    resume_path = default_resume(profile)
    upload_state = {"done": False}
    held_list = []
    fill_memo = set()     # dropdown/radio/chiplist filled this run -> don't re-open
    filled_all = []       # every field filled this page (across attempts) -> returned

    print("=" * 72)
    print(f"  CONVERGE PAGE   (max {max_attempts} attempts)   resume={bool(resume_path)}")
    print("=" * 72)

    async def _live(tag=""):
        """Push a single frame to the UI (sequential; failures are silent)."""
        if not on_screenshot:
            return
        try:
            sp = f"output/converge_{user_id}_{tag}.png"
            await page.screenshot(path=sp)
            await on_screenshot(sp)
        except Exception:
            pass

    prev_sig = None
    grounded_tried = False   # on-demand HTML+vision repair: at most once per page
    field_repairs = 0        # field-level HTML+vision repairs (the "retry 2x" budget)
    home_dom = _site_domain(page.url)

    for attempt in range(1, max_attempts + 1):
        print(f"\n--- attempt {attempt} ---")
        held_list.clear()

        if attempt > 1:  # a leftover popup can only exist after a prior attempt
            try:  # dismiss any stray native <select> popup left by a mis-click —
                await page.keyboard.press("Escape")  # it eats every click until closed
            except Exception:
                pass

        # Origin guard: a mis-click on an in-form link (privacy policy,
        # arbitration agreement…) can navigate AWAY from the application —
        # live test wandered from Greenhouse onto glean.com's marketing site.
        # If the domain changed, go back to the form before doing anything.
        if home_dom and _site_domain(page.url) != home_dom:
            print(f"  wandered off-application → {page.url[:70]} — going back")
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=8000)
                await settle(page)
            except Exception:
                pass

        # 0a) clear cookie/consent banners + stuck modal backdrops that swallow
        #     clicks. Generic + deterministic; handles banners that reappear on
        #     new tabs / cross-domain hops mid-flow (external portals esp.).
        clicked = await dismiss_overlays(page)
        if clicked:
            print(f"  dismissed overlay: {clicked!r}")
        removed = await clear_blocking_overlays(page)
        if removed:
            print(f"  cleared {removed} stuck backdrop(s)")

        # 0) delete phantom rows
        while await _delete_phantom_rows(page, profile):
            print("  deleted a phantom row")

        # 1) fill — v2 (HTML -> LLM -> CSS selectors), with v1 fallback
        b = await _fill_pass_v2(page, profile, user_id, creds, resume_path,
                                upload_state, gemini_client, held_list, fill_memo)
        if b is None:
            b = await _fill_pass(page, profile, user_id, creds, resume_path,
                                 upload_state, gemini_client, held_list, fill_memo)
        print(f"  filled {len(b['filled'])}; {len(b['nodata'])} no-data; "
              f"{len(b['fail'])} fail; {len(b['held'])} held")
        for n in b["filled"]: print(f"    OK   {n}")
        for n in b["held"]:   print(f"    HOLD {n}")
        for n in b["nodata"]: print(f"    --   {n}")
        for n in b["fail"]:   print(f"    FAIL {n}")
        for n in b["filled"]:
            if n not in filled_all:
                filled_all.append(n)

        # live: announce each field as it fills + push a frame (sequential = safe)
        if on_notify:
            for n in b["filled"]:
                try: await on_notify(f"✓ {n}")
                except Exception: pass
            # RC4: surface fields we could NOT fill (no profile data / failed) so
            # the run isn't silently "thinking" while required fields sit empty.
            # nodata/fail were previously console-only.
            for n in (b["nodata"] + b["fail"])[:6]:
                try: await on_notify(f"⚠ couldn't fill {n}")
                except Exception: pass
        await _live(f"a{attempt}")

        # Universal field-level recovery (LEAK #1 fix): any field the normal fill
        # couldn't set gets the HTML+vision grounded repair AT THE POINT OF
        # FAILURE — not only later on repeated validation errors, and targeting
        # the ACTUAL failed field (not an error-text guess). This is the path that
        # was missing for custom widgets like the phone-country dropdown. Bounded
        # to a small "retry" budget per page.
        if b["fail"] and field_repairs < 2:
            fail_labels = [l for l in (_label_of_note(n) for n in b["fail"]) if l]
            if fail_labels:
                field_repairs += 1
                # Phone-country failures: the deterministic exact-match handler is
                # FAR more reliable than generic LLM grounding here (the LLM
                # mis-picks 'British Indian Ocean Territory' for 'India'). Try it
                # first; only fall back to grounding if it didn't apply.
                if any(("phone" in l.lower() and "country" in l.lower())
                       or l.lower() in ("country", "phone country", "phone country dropdown")
                       for l in fail_labels):
                    if await _fix_phone_country(page, profile, on_notify=on_notify):
                        continue   # set → re-run the loop (re-fill is now guarded)
                if on_notify:
                    await on_notify(f"🔎 Looking closer at: {', '.join(fail_labels[:3])}")
                try:
                    gfix = await _grounded_repair(
                        page, profile, user_id, creds, gemini_client,
                        [{"label": l} for l in fail_labels])
                    if gfix:
                        print(f"  -> field-level grounded repair fixed {gfix} field(s)")
                except Exception as ex:
                    print(f"  (field grounded repair skipped: {ex})")

        # 2) advance
        before = page.url
        before_step = await _step_sig(page)   # multi-step SPA signal (Workday)
        ok, why = await advance(page)
        print(f"  advance: {'clicked' if ok else 'NOT clicked'} ({why})")
        await page.wait_for_timeout(2500)
        np = await switch_if_new_tab(ctx, page)
        if np is not page:
            print(f"  -> new tab: {np.url[:70]}")
            page = np
        await settle(page)

        # Advanced if the URL changed OR — for single-URL SPAs like Workday, where
        # every step shares /apply/applyManually — the form's active step/heading
        # changed. Without the step check, a normal Save-and-Continue on Workday
        # looks like "didn't advance" and the page limps forward via fallbacks.
        after_step = await _step_sig(page)
        if page.url != before or (before_step and after_step and after_step != before_step):
            print(f"\n[OK] PAGE CLEARED — advanced to:\n     {page.url}  step={after_step[:40]!r}")
            return {"status": "advanced", "page": page, "url": page.url, "held": held_list, "filled": filled_all}

        # 3) blocked -> the page errors are the to-do list
        errors = await scan_page_errors(page)
        if not errors:
            print("\n[?] Didn't advance but no validation errors found.")
            print("    Likely: modal open, SUBMIT review page, or slow network.")
            _print_held_summary(held_list, user_id)
            return {"status": "stuck_no_errors", "page": page, "url": page.url, "held": held_list, "filled": filled_all}

        print(f"\n[!] {len(errors)} blocking error(s):")
        for e in errors[:12]:
            mk = f"[{e['idx']:>3}] " if e.get("idx") is not None else "      "
            print(f"    {mk}{e.get('label','')!r:<38} {e.get('error','')}")

        # Phone-country first: "phone number is too long / invalid" is almost
        # always a WRONG country code (e.g. +246 instead of +91), and the fix is
        # the COUNTRY selector, not the number — which generic field correction
        # would never touch. Handle it deterministically before anything else.
        if any("phone" in (str(e.get("error", "")) + " " + str(e.get("label", ""))).lower()
               for e in errors):
            if await _fix_phone_country(page, profile, on_notify=on_notify):
                print("  -> fixed phone country deterministically")
                prev_sig = None
                continue

        print("\n  correcting errored fields:")
        n_fixed = await _correct_errored_fields(page, profile, user_id, creds,
                                                gemini_client, held_list, fill_memo)
        print(f"  -> corrected {n_fixed} field(s)")
        if n_fixed > 0:
            prev_sig = None
            continue

        sig = tuple(sorted(e.get("label", "").lower() for e in errors))
        if sig == prev_sig:
            # On-demand "look closer": deterministic correction stalled on the same
            # errors. Capture the failing fields' HTML + a screenshot and let the
            # LLM ground a fix (handles novel widgets we haven't hard-coded). Bounded
            # to ONCE per page; if it commits anything, retry the loop.
            if not grounded_tried:
                grounded_tried = True
                print("\n  on-demand grounded repair (HTML + vision)…")
                g = await _grounded_repair(page, profile, user_id, creds, gemini_client, errors)
                print(f"  -> grounded repair committed {g} field(s)")
                if g > 0:
                    prev_sig = None
                    continue
            print("\n[STOP] Same errors and nothing corrected. Need manual entry / data:")
            for e in errors[:12]:
                print(f"         - {e.get('label','')}")
            _print_held_summary(held_list, user_id)
            return {"status": "stuck", "page": page, "url": page.url,
                    "errors": errors, "held": held_list, "filled": filled_all}
        prev_sig = sig

    print(f"\n[STOP] Hit max {max_attempts} attempts.")
    _print_held_summary(held_list, user_id)
    return {"status": "max_attempts", "page": page, "url": page.url, "held": held_list, "filled": filled_all}


# ── gateway / landing-page advance (productionized from the debug notebooks) ──
_GATEWAY_PROMPT = """You are triaging ONE page in a job-application flow.
Red numbered boxes mark interactive elements (the number is each element's index).

Return STRICT JSON only:
{
  "page_type": "<gateway | form | login | other>",
  "advance":   {"index": <int or null>, "label": "<button text or empty>"}
}

DEFINITIONS
- gateway = a landing / job-description page whose ONLY purpose is to send you
  forward. The action is a single button like "Apply", "Apply Now",
  "Apply Manually", "Start Application", "Continue to application".
- form  = a page with fields to fill.   - login = sign-in / create-account page.
- other = none of the above.
Set advance.index ONLY for a gateway page; otherwise advance.index = null.
NEVER pick Sign Out, header nav, footer, language, social, cookie, or OAuth buttons.
"Apply With LinkedIn", "Sign in with Google", or any social-login / OAuth button is NOT
a valid gateway advance — those redirect to third-party auth flows, not the application form.
Prefer "Apply Manually", "Apply Now", or "Start Application" over any OAuth option.
Return ONLY the JSON object."""


async def gateway_advance(page, ctx, gemini_client=None, *, on_notify=None, max_hops=3):
    """Landing / job-description pages whose only job is to send you to the real
    application: detect via ONE screenshot LLM call, click Apply, follow the new
    tab. Returns the (possibly new) page. No-op on form/login pages."""
    creds = {"email": os.getenv("APPLY_EMAIL", ""), "password": os.getenv("APPLY_PASSWORD", "")}
    for _hop in range(max_hops):
        await dismiss_overlays(page)
        await clear_blocking_overlays(page)
        await settle(page)
        try:
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        await page.wait_for_timeout(300)
        elems, idx_fr = await collect_elements(page)
        try:
            raw = await page.screenshot(full_page=True)
        except Exception:
            raw = await page.screenshot()
        img = base64.b64encode(annotate_screenshot(raw, elems)).decode()
        info = llm_json(_GATEWAY_PROMPT, image_b64=img,
                        gemini_client=gemini_client, gemini_model=FLASH_MODEL) or {}
        ptype = (info.get("page_type") or "other").lower()
        adv = info.get("advance") or {}
        if ptype != "gateway" or adv.get("index") is None:
            return page
        before = page.url
        # Set-of-marks vision sometimes returns the right LABEL with the wrong
        # NUMBER (live test: claimed 'Apply Now', index pointed at the 'Teams'
        # nav link). If the indexed element's own label disagrees with the
        # claimed label, re-resolve the index by label.
        adv_idx = adv.get("index")
        claimed = (adv.get("label") or "").strip().lower()
        by_idx  = next((el for el in elems if el.get("idx") == adv_idx), None)
        actual  = ((by_idx or {}).get("label") or "").strip().lower()
        if claimed and actual and claimed not in actual and actual not in claimed:
            match = next((el for el in elems
                          if claimed in ((el.get("label") or "").strip().lower())), None)
            if match:
                print(f"  gateway: index {adv_idx} is {actual!r} — re-resolved to "
                      f"[{match.get('idx')}] by label {claimed!r}")
                adv_idx = match.get("idx")
        if on_notify:
            await on_notify(f"🚪 Gateway page — clicking '{adv.get('label', 'Apply')}'")
        try:
            await execute_action(page, {"action": "click", "index": adv_idx,
                                        "label": adv.get("label", "")},
                                 idx_fr, elems, "", creds, gateway=True)
        except Exception:
            return page
        await page.wait_for_timeout(2500)
        np = await switch_if_new_tab(ctx, page)
        if np is not page:
            page = np
        await settle(page)
        # Gateway clicked a legal/policy link instead of Apply (vision mis-pick)
        # — undo immediately; these pages are never the application.
        u = page.url.lower()
        if page.url != before and any(k in u for k in
                ("arbitration", "privacy", "terms", "cookie", "policy",
                 "user-agreement", "linkedin.com/legal", "linkedin.com/uas")):
            print(f"  gateway landed on legal page {page.url[:60]} — going back")
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=8000)
                await settle(page)
            except Exception:
                pass
            continue
        if page.url == before and np is page:
            return page   # nothing moved — stop
    return page


# ── multi-row reveal (productionized from the debug notebooks) ───────────────
_REVEAL_SECTIONS = {
    "experience":     ("work experience", "employment", "experience"),
    "education":      ("education",),
    "certifications": ("certification", "certifications"),
}

def _is_add_button(e):
    lab = (e.get("label") or "").strip().lower()
    return e.get("tag") in ("button", "a") and (
        lab == "add" or lab == "+ add" or lab.startswith("add ") or "add another" in lab)

def _find_add_button(elems, header_words):
    """Best 'Add' button for a section. Prefer one whose own section tag matches
    the section; else the first Add on the page (most pages scope Add per section)."""
    cands = [e for e in elems if _is_add_button(e)]
    if not cands:
        return None
    for e in cands:
        sec = (e.get("section_label") or e.get("section_word") or "").lower()
        if any(h in sec for h in header_words):
            return e
    return cands[0]

async def reveal_rows(page, ctx, profile, gemini_client=None, *, on_notify=None, max_passes=6):
    """Click 'Add' until each multi-row section has as many rows as the profile
    needs. Counts visible rows via _assign_section_rows (handles un-numbered /
    resume-auto-parsed rows), so it adds only the gap and never blindly duplicates.
    Deterministic; one Add per section per pass, then re-scan. Returns page."""
    for _ in range(max_passes):
        elems, _ = await collect_elements(page)
        _assign_section_rows(elems)
        have = {}
        for e in elems:
            p = _parse_sec(e.get("section_label"))
            if p:
                have.setdefault(p[0], set()).add(p[1])
        have = {k: len(v) for k, v in have.items()}

        needed = [(plist, hdrs) for plist, hdrs in _REVEAL_SECTIONS.items()
                  if len(profile.get(plist) or []) > have.get(plist, 0)]
        if not needed:
            return page

        creds = {"email": os.getenv("APPLY_EMAIL", ""), "password": os.getenv("APPLY_PASSWORD", "")}
        clicked_any = False
        for plist, hdrs in needed:
            btn = _find_add_button(elems, hdrs)
            if btn is None:
                continue
            if on_notify:
                await on_notify(f"➕ Adding a {plist.rstrip('s')} row "
                                f"(have {have.get(plist, 0)}/{len(profile.get(plist) or [])})")
            try:
                await execute_action(page, {"action": "click", "index": btn["idx"],
                                            "label": btn.get("label", "Add")},
                                     None, elems, "", creds)
                await settle(page)
                clicked_any = True
            except Exception:
                continue
        if not clicked_any:
            return page   # no Add buttons -> section not on this page / maxed out
    return page


# ── on-demand grounded repair (HTML + vision) — Tier-4 escalation ────────────
# Fires ONLY when deterministic correction has stalled on the same errors. For
# each failing field it grabs that element's outerHTML + a full-page screenshot
# and asks the LLM how to operate it, then performs a grounded action — including
# a DIRECT option-click that bypasses click_option's heuristics, for novel /
# never-seen widgets. This is the "look closer only when stuck" step.
async def _element_html(page, idx, max_len=4000):
    try:
        h = await page.locator(f'[data-agent-idx="{idx}"]').first.evaluate(
            "e => (e.closest('[class*=control],[role=combobox],[class*=select],[class*=Select]') || e).outerHTML")
        return (h or "")[:max_len]
    except Exception:
        return ""


async def _click_option_by_text(page, want):
    """Directly click an OPEN dropdown's option whose visible text matches `want`,
    scrolling the list as needed. Bypasses click_option's heuristics — used by the
    grounded-repair path for widgets the normal selectors can't drive."""
    want_l = _deaccent(want)
    sels = ["[role='option']", "[data-automation-id='promptOption']",
            "[data-automation-id='promptLeafNode']", ".select__option",
            "[id*='-option-']", "li[role='option']", "[class*='option']:not(button)",
            # Phone-country widgets render options as plain <li> (no role=option,
            # no 'option' class) — include their library markup + a generic <li>
            # catch-all so the HTML-grounded fallback can drive them.
            "li.iti__country", ".iti__country-list li", ".country-list li",
            "[class*='country-selector'] li", "[class*='country'] li",
            "[class*='dropdown'] li", "[class*='menu'] li", "li"]
    for _ in range(12):
        # Two-pass per scroll position: take an EXACT (deaccented) text match if one
        # exists before settling for a substring hit. 'India' is a substring of
        # 'British Indian Ocean Territory' (+246) — substring-first picks the wrong
        # country; exact-first fixes it.
        substr_fallback = None
        for s in sels:
            try:
                loc = page.locator(s)
                for i in range(min(await loc.count(), 80)):
                    o = loc.nth(i)
                    try:
                        if not await o.is_visible():
                            continue
                        t = _deaccent((await o.inner_text()) or "")
                    except Exception:
                        continue
                    if not t:
                        continue
                    base = t.split("+")[0].strip()   # drop a trailing dial code
                    if t == want_l or base == want_l or t.startswith(want_l + " "):
                        try:
                            await o.scroll_into_view_if_needed(timeout=1200)
                            await o.click(timeout=3000)
                            await page.wait_for_timeout(400)
                            return True
                        except Exception:
                            continue
                    if substr_fallback is None and (want_l in t or t in want_l):
                        substr_fallback = o
            except Exception:
                continue
        if substr_fallback is not None:
            try:
                await substr_fallback.scroll_into_view_if_needed(timeout=1200)
                await substr_fallback.click(timeout=3000)
                await page.wait_for_timeout(400)
                return True
            except Exception:
                pass
        try:
            await page.locator("[role='listbox']").last.evaluate(
                "el => el.scrollBy(0, Math.max(el.clientHeight*0.8, 240))")
        except Exception:
            try:
                await page.mouse.wheel(0, 420)
            except Exception:
                break
        await page.wait_for_timeout(220)
    return False


_GROUNDED_PROMPT = """A form field will not accept its value. Here is the field's HTML
and a full-page screenshot (red numbered boxes mark elements; the number is the index).

FIELD LABEL : {label}
TARGET VALUE (from the candidate's profile): {value}
FIELD HTML  :
{html}

Decide how to set it. Return STRICT JSON:
{{"action": "<select|fill|click|none>", "value": "<exact value / option label to use>", "open_index": <int or null>, "reason": "<short>"}}
- Dropdown / listbox / combobox  -> action="select"; value = the EXACT option label that
  best matches the target (copy it verbatim if it appears in the HTML); open_index = the
  red-box index to click to OPEN the menu (or null to open the field itself).
- Text box / textarea            -> action="fill"; value = the text to type.
- Checkbox / toggle to tick      -> action="click".
- If you cannot tell             -> action="none".
NEVER choose Submit / Sign out / Create account. Return ONLY the JSON object."""


async def _grounded_repair(page, profile, user_id, creds, gemini_client, errors):
    """One bounded on-demand pass: locate errored fields, and for each, ground a
    fix from its HTML + the screenshot. Returns the count of fields committed."""
    elements, idx_frame = await collect_elements(page)
    _assign_section_rows(elements)
    err_texts = [t for t in
                 ((_norm_lbl(e.get("label")) or _norm_lbl(e.get("error"))) for e in (errors or []))
                 if t]
    targets, seen = [], set()
    for el in elements:
        if classify_field(el) == "skip":
            continue
        lab = _norm_lbl(el.get("label"))
        if not lab:
            continue
        if any(lab == t or (len(lab) >= 4 and (lab in t or t in lab)) for t in err_texts):
            if el.get("idx") not in seen:
                seen.add(el.get("idx")); targets.append(el)
    if not targets:
        return 0
    try:
        raw = await page.screenshot(full_page=True)
    except Exception:
        raw = await page.screenshot()
    img = base64.b64encode(annotate_screenshot(raw, elements)).decode()
    fixed = 0
    for e in targets[:4]:
        idx, label = e.get("idx"), e.get("label", "")
        val = resolve_field_value(e, profile) or ""
        html = await _element_html(page, idx)
        try:
            plan = llm_json(
                _GROUNDED_PROMPT.format(label=label, value=val or "(derive from the profile)", html=html),
                image_b64=img, gemini_client=gemini_client, gemini_model=FLASH_MODEL) or {}
        except Exception:
            continue
        act = (plan.get("action") or "none").lower()
        pv = (plan.get("value") or val or "").strip()
        low = label.lower()
        if act == "none" or not pv:
            continue
        if act == "click" and any(x in low or x in pv.lower()
                                  for x in ("submit", "sign out", "log out", "create account")):
            continue
        try:
            if act == "select":
                open_idx = plan.get("open_index")
                open_idx = open_idx if open_idx is not None else idx
                await execute_action(page, {"action": "click", "index": open_idx,
                                            "label": label}, idx_frame, elements, "", creds)
                await page.wait_for_timeout(500)
                ok = await _click_option_by_text(page, pv)
                if not ok:   # fall back to the normal select path with the LLM's exact label
                    ok, _ = await execute_action(page, {"action": "select", "index": idx,
                             "value": pv, "label": label}, idx_frame, elements, "", creds)
            else:
                ok, _ = await execute_action(page, {"action": act, "index": idx,
                         "value": pv, "label": label}, idx_frame, elements, "", creds)
            if ok:
                fixed += 1
        except Exception:
            pass
        await page.wait_for_timeout(200)
    return fixed
