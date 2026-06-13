"""
apply_skills/base.py — Shared action dispatcher used by all skills.
Same 10-action approach as linkedin_easy_apply.py but fully standalone.
"""

import json
import logging
import re
from pathlib import Path
from typing import Callable

from profile_manager import learn_answer

logger = logging.getLogger(__name__)


def json_config():
    """GenerateContentConfig that forces the model to return raw JSON.
    Prevents skills from silently no-opping when the model wraps JSON in prose."""
    from google.genai import types
    return types.GenerateContentConfig(response_mime_type="application/json")


async def dispatch_action(page, action: dict, resume_path: str,
                          on_stuck: Callable = None,
                          filled_selectors: set = None,
                          user_id: int = None) -> bool:
    """
    Execute a single Gemini-planned browser action.
    Returns True if action succeeded, False otherwise.
    When user_id is given, answers the user types for unknown fields are
    persisted via learn_answer so they auto-fill on future applications.
    """
    if filled_selectors is None:
        filled_selectors = set()

    act      = (action.get("action") or "").strip().lower()
    selector = (action.get("selector") or "").strip()
    value    = action.get("value")
    label    = action.get("label") or selector or act

    if not act:
        return False

    # ── wait ──────────────────────────────────────────────────────────────────
    if act == "wait":
        await page.wait_for_timeout(int(value or 800))
        return True

    if selector in filled_selectors:
        logger.debug(f"  Already handled: {selector}")
        return False

    # ── Resolve element — search main frame then child frames, skip hidden ─────
    el = None
    el_frame = page  # the context (Page or Frame) where element was found
    if selector:
        for frame in page.frames:
            try:
                els = frame.locator(selector)
                count = await els.count()
                for i in range(count):
                    candidate = els.nth(i)
                    # Prefer visible elements, but accept any if none visible
                    if await candidate.is_visible():
                        el = candidate
                        el_frame = frame
                        break
                if el is None and count > 0:
                    el = els.first
                    el_frame = frame
                if el is not None:
                    break
            except Exception:
                continue

    if el is None and act != "wait":
        logger.debug(f"  Element not found: {selector} [{label}]")
        return False

    # ── Ask user if value missing for fill-type actions ────────────────────────
    needs_value = act in ("fill", "click_option", "clear_and_fill", "press_sequentially")
    if needs_value and not value:
        if on_stuck:
            value = await on_stuck(label)
            if not value or value.lower() == "skip":
                return False
            if user_id:  # remember this answer for next time
                try:
                    learn_answer(user_id, label, value)
                    logger.info(f"  Learned: {label} = {str(value)[:40]}")
                except Exception:
                    pass
        else:
            return False

    try:
        if act == "scroll_into_view":
            await el.scroll_into_view_if_needed()

        elif act == "hover":
            await el.hover()

        elif act == "press_key":
            await el.press(str(value))

        elif act == "upload":
            path = resume_path  # always use resume_path, ignore Gemini value
            if path and Path(path).exists():
                await el.set_input_files(path)
                logger.info(f"  Uploaded: {Path(path).name} [{label}]")
                filled_selectors.add(selector)
                await page.wait_for_timeout(800)
                return True
            else:
                logger.warning(f"  Upload path not found: {path}")
                return False

        elif act == "fill":
            # Skip disabled elements immediately (don't wait 30s)
            if not await el.is_enabled():
                logger.debug(f"  Skipping disabled field: {label}")
                return False
            await el.scroll_into_view_if_needed()
            await el.fill(str(value), timeout=8000)
            # Fire blur so React/Workday-style async validation runs (this is what
            # lets the page's "Create Account / Continue" button become enabled).
            try:
                await el.blur()
            except Exception:
                pass
            logger.info(f"  Filled: {label} = {str(value)[:50]}")
            await page.wait_for_timeout(400)
            filled_selectors.add(selector)
            return True

        elif act == "clear_and_fill":
            if not await el.is_enabled():
                logger.debug(f"  Skipping disabled field: {label}")
                return False
            await el.scroll_into_view_if_needed()
            await el.click(click_count=3)
            await el.press("Control+a")
            await el.press("Backspace")
            await el.fill(str(value), timeout=8000)
            logger.info(f"  Clear+Fill: {label} = {str(value)[:50]}")
            await page.wait_for_timeout(400)
            filled_selectors.add(selector)
            return True

        elif act == "press_sequentially":
            if not await el.is_enabled():
                logger.debug(f"  Skipping disabled field: {label}")
                return False
            await el.scroll_into_view_if_needed()
            await el.click(click_count=3)
            await el.press_sequentially(str(value), delay=60)
            logger.info(f"  Typed: {label} = {str(value)[:50]}")
            await page.wait_for_timeout(600)
            filled_selectors.add(selector)
            return True

        elif act == "click":
            await el.scroll_into_view_if_needed()
            # Clicking a native <select> opens the OS popup Playwright can't
            # drive — the planner sometimes emits 'click' for them; refuse.
            tag = (await el.evaluate("e => e.tagName")).lower()
            if tag == "select":
                logger.info(f"  Refusing raw click on native <select> [{label}]")
                return False
            # NEVER click legal/policy links — they navigate off the form.
            # (Vision recovery once clicked Glean's "Applicant Arbitration
            # Agreement" because the form text says "I confirm I have read…".)
            # No LLM decision may override this.
            if True:  # any element may sit INSIDE a link (<strong> in <a>) — check closest anchor
                href = (await el.evaluate(
                    "e => { const a = e.closest ? e.closest('a') : null;"
                    " return a ? (a.href || '') : (e.href || ''); }") or "").lower()
                txt = (label or "").lower()
                _legal = ("arbitration", "privacy", "terms", "policy", "cookie",
                          "agreement", "definitions", "gdpr", "disclosure")
                if any(k in href or k in txt for k in _legal):
                    logger.info(f"  Refusing click on legal/policy link [{label}]")
                    return False
                if href.startswith("http"):
                    from urllib.parse import urlparse
                    h, p = urlparse(href).netloc, urlparse(page.url).netloc
                    if h and p and h.split(".")[-2:] != p.split(".")[-2:]:
                        logger.info(f"  Refusing click on off-site link [{label}] → {href[:50]}")
                        return False
            await el.click()
            logger.info(f"  Clicked: {label}")
            await page.wait_for_timeout(400)
            filled_selectors.add(selector)
            return True

        elif act == "click_option":
            await el.scroll_into_view_if_needed()
            tag = (await el.evaluate("e => e.tagName")).lower()
            if tag == "select":
                val_s = str(value or "").strip()
                _ph = ("please select", "select one", "select an option", "choose", "--")
                def _is_placeholder(o):
                    ol = o.lower().strip()
                    return any(ol == p or ol.startswith(p) for p in _ph)
                options = [o.strip() for o in await el.locator("option").all_inner_texts() if o.strip()]
                best = None
                if val_s:  # empty value must NOT match (''-in-anything is always true)
                    best = next(
                        (o for o in options if not _is_placeholder(o)
                         and (val_s.lower() in o.lower() or o.lower() in val_s.lower())),
                        None
                    )
                if best:
                    await el.select_option(label=best, timeout=5000)
                    logger.info(f"  Native select: {label} = {best}")
                    filled_selectors.add(selector)
                    return True
                # NEVER fall through to the combobox path for a native <select>:
                # clicking it opens the OS-level select popup, which Playwright
                # cannot drive and which blocks every later action on the page.
                logger.info(f"  Native select: no option match for {value!r} [{label}]")
                return False

            # ── Custom combobox (Workday / React listboxes) ─────────────────
            # Strategy: open dropdown -> try a series of search terms (most
            # discriminating first) -> rank visible options -> click best ->
            # VERIFY the field actually shows a value afterward. Verification
            # is essential because click_option previously returned True even
            # when the click hit a stale option from another popup.
            val_full = str(value).strip()
            val_low  = val_full.lower()

            # Build search-term ladder. For "+91 India" we want to try:
            #   1. "+91 India"      (in case the option label is verbatim)
            #   2. "India"          (country name word — most discriminating)
            #   3. "91"             (numeric code)
            # Deduplicate, drop empties, cap at 4 attempts.
            _words  = re.findall(r"[a-zA-Z]{3,}", val_full)
            _digits = re.findall(r"\d{2,}", val_full)
            search_terms, _seen = [], set()
            for t in [val_full] + _words + _digits:
                k = t.lower().strip()
                if k and k not in _seen:
                    _seen.add(k); search_terms.append(t)
            search_terms = search_terms[:4]

            option_sels = [
                "[data-automation-id='promptOption']",      # Workday
                "[data-automation-id='promptLeafNode']",    # Workday nested
                "[role='listbox'] [role='option']",
                "[role='listbox'] li",
                "[role='option']",
                "ul[role='listbox'] li",
                "li[role='option']",
                "[class*='option']:not(button)",
            ]

            async def _scrape_visible_options():
                cands = []   # (rank, len, locator, text)
                for fr in page.frames:
                    for list_sel in option_sels:
                        try:
                            opts = fr.locator(list_sel)
                            for i in range(min(await opts.count(), 60)):
                                o = opts.nth(i)
                                try:
                                    if not await o.is_visible():
                                        continue
                                except Exception:
                                    continue
                                txt = ((await o.inner_text()) or "").replace("\n", " ").strip()
                                if not txt or len(txt) > 200:
                                    continue
                                t = txt.lower()
                                if t == val_low:
                                    rank = 0
                                elif t.startswith(val_low) or val_low.startswith(t):
                                    rank = 1
                                elif re.search(r"\b" + re.escape(val_low) + r"\b", t):
                                    rank = 2
                                elif val_low in t or any(w.lower() in t for w in _words):
                                    rank = 3
                                else:
                                    continue
                                cands.append((rank, len(t), o, txt))
                        except Exception:
                            continue
                return cands

            async def _open_dropdown():
                try:
                    await el.click()
                except Exception:
                    pass
                # Wait longer for the popup itself to render before we type.
                await page.wait_for_timeout(700)

            async def _close_dropdown():
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(200)
                except Exception:
                    pass

            async def _clear_typed():
                """Clear what we typed so the next attempt starts fresh."""
                try:
                    # Ctrl+A then Backspace is faster + more reliable than 40
                    # individual backspaces (which can drift if focus shifts).
                    await page.keyboard.press("Control+A")
                    await page.wait_for_timeout(80)
                    await page.keyboard.press("Backspace")
                    await page.wait_for_timeout(120)
                except Exception:
                    pass

            async def _wait_for_options(max_ms=3500, poll_ms=250):
                """Poll until visible options appear in the popup, or timeout.
                Workday's typeahead has a ~300-500ms debounce on the input
                PLUS the lazy-render of filtered rows — a fixed wait either
                short-changes us or burns time. Polling matches the real
                arrival of options."""
                elapsed = 0
                last_cands = []
                while elapsed < max_ms:
                    cands = await _scrape_visible_options()
                    if cands:
                        # Once we see options, give one more tick for the
                        # full filtered list to settle before picking.
                        await page.wait_for_timeout(200)
                        return await _scrape_visible_options()
                    last_cands = cands
                    await page.wait_for_timeout(poll_ms)
                    elapsed += poll_ms
                return last_cands

            async def _scroll_hunt():
                """Long / virtualised listboxes that DON'T filter on type: the
                target option is below the fold (or not yet rendered). Scroll the
                open listbox container in steps, re-scraping each step, until an
                exact/prefix match appears or the list stops yielding new options.
                Returns the best candidate found, or None."""
                container = None
                for csel in ("[role='listbox']", "ul[role='listbox']",
                             "[data-automation-id*='opup']", "[class*='menu'][class*='list']"):
                    try:
                        loc = page.locator(csel).last
                        if await loc.count() and await loc.is_visible():
                            container = loc
                            break
                    except Exception:
                        continue
                seen_texts: set = set()
                for _ in range(14):
                    cands = await _scrape_visible_options()
                    strong = [c for c in cands if c[0] <= 1]   # exact / prefix
                    if strong:
                        strong.sort(key=lambda c: (c[0], c[1]))
                        return strong[0]
                    texts = {c[3] for c in cands}
                    if texts and texts <= seen_texts:
                        break                                   # no new rows → end of list
                    seen_texts |= texts
                    scrolled = False
                    if container is not None:
                        try:
                            await container.evaluate(
                                "el => el.scrollBy(0, Math.max(el.clientHeight*0.8, 240))")
                            scrolled = True
                        except Exception:
                            pass
                    if not scrolled:
                        try:
                            await page.mouse.wheel(0, 420)      # fallback: wheel over the list
                        except Exception:
                            break
                    await page.wait_for_timeout(250)
                cands = await _scrape_visible_options()
                cands.sort(key=lambda c: (c[0], c[1]))
                return cands[0] if cands else None

            async def _read_field_value():
                """Best-effort read of what the field currently shows."""
                txt = ""
                try:
                    txt = ((await el.inner_text()) or "").strip()
                except Exception:
                    pass
                if not txt or txt.lower() == "select one":
                    try:
                        txt = (await el.evaluate(
                            "e => (e.value || (e.querySelector('input,select') "
                            "? (e.querySelector('input,select').value || '') : '')).toString()"
                        )) or ""
                    except Exception:
                        pass
                return (txt or "").strip()

            # ── react-select / Greenhouse custom dropdown (high-priority path) ──
            # Structure: a combobox <input> inside .select__control; options render
            # as .select__option / [role=option] / [id$="-option-N"] in a (portalled)
            # .select__menu; the COMMITTED value lands in .select__single-value while
            # the input CLEARS. The generic ladder misses these because it verifies
            # the input text (which clears) and some variants are click-only (not
            # searchable). So: open the control, type if searchable, click the
            # best-matching option, and verify the SINGLE-VALUE (accept even when it
            # equals the typed term — that's the whole bug for Yes/No, India, etc.).
            try:
                _is_rs = await el.evaluate(
                    """e => {
                        const ctrl = e.closest('.select__control')
                            || (e.parentElement && e.parentElement.closest('.select__control'))
                            || (e.querySelector ? e.querySelector('.select__control') : null);
                        return !!ctrl || /\\bselect__/.test(e.className || '');
                    }""")
            except Exception:
                _is_rs = False

            if _is_rs:
                async def _rs_open():
                    try:
                        await el.evaluate(
                            """e => {
                                const ctrl = e.closest('.select__control')
                                    || (e.parentElement && e.parentElement.closest('.select__control')) || e;
                                ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                            }""")
                    except Exception:
                        pass
                    try:
                        await el.click()
                    except Exception:
                        pass
                    await page.wait_for_timeout(450)

                async def _rs_committed():
                    try:
                        return (await el.evaluate(
                            """e => {
                                const ctrl = e.closest('.select__control')
                                    || (e.parentElement && e.parentElement.closest('.select__control'))
                                    || (e.querySelector ? e.querySelector('.select__control') : null) || e;
                                const sv = ctrl.querySelector('.select__single-value,[class*=singleValue]');
                                return sv ? (sv.innerText || sv.textContent || '').trim() : '';
                            }""")) or ""
                    except Exception:
                        return ""

                rs_terms, _seen_rs = [], set()
                for t in [val_full] + _words:
                    k = t.lower().strip()
                    if k and k not in _seen_rs:
                        _seen_rs.add(k); rs_terms.append(t)
                for term in (rs_terms[:3] or [val_full]):
                    await _rs_open()
                    try:   # type to filter (no-op on click-only variants)
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Backspace")
                        await page.keyboard.type(term, delay=45)
                        await page.wait_for_timeout(400)
                    except Exception:
                        pass
                    cands = await _wait_for_options(max_ms=2500, poll_ms=200)
                    if not cands or cands[0][0] >= 2:        # weak/none → maybe a long list
                        h = await _scroll_hunt()
                        if h is not None and (not cands or h[0] < cands[0][0]):
                            cands = [h] + (cands or [])
                    if cands:
                        cands.sort(key=lambda c: (c[0], c[1]))
                        _, _, o, txt = cands[0]
                        try:
                            await o.scroll_into_view_if_needed(timeout=1500)
                            await o.click(timeout=4000)
                            await page.wait_for_timeout(500)
                        except Exception:
                            pass
                    cv = await _rs_committed()
                    if cv and cv.lower() not in ("select one", ""):
                        # Blur so react-select fires its onChange/onBlur and the
                        # form's validation actually registers the value (otherwise
                        # it can DISPLAY 'Yes' yet still flag the field 'required').
                        try:
                            await page.keyboard.press("Escape")
                            await el.evaluate("e => { const i = e.querySelector('input'); (i||e).blur && (i||e).blur(); }")
                            await page.wait_for_timeout(200)
                        except Exception:
                            pass
                        logger.info(f"  click_option[react-select]: {label} = {cv[:40]}")
                        filled_selectors.add(selector)
                        await page.wait_for_timeout(200)
                        return True
                    await _close_dropdown()
                logger.debug("  click_option: react-select path didn't commit; trying generic ladder")

            picked_txt = None
            for term in search_terms:
                await _open_dropdown()
                # IMPORTANT: clear whatever the dropdown's input already
                # contains BEFORE we type. Workday's typeahead opens with the
                # previously-selected value pre-filled in the search box —
                # if we type on top of that, we append (e.g. "+91India"),
                # the filter shows nothing, and we end up wiping the
                # selection entirely.
                try:
                    await page.keyboard.press("Control+A")
                    await page.wait_for_timeout(80)
                    await page.keyboard.press("Backspace")
                    await page.wait_for_timeout(150)
                except Exception:
                    pass
                try:
                    # Slower per-keystroke delay (60ms) so Workday's typeahead
                    # debounce reliably triggers — 35ms was sometimes faster
                    # than the framework's input throttling.
                    await page.keyboard.type(term, delay=60)
                    await page.wait_for_timeout(450)
                except Exception:
                    pass

                # ── Strategy 1: press Enter to let Workday's typeahead commit
                # the typed term as a selection (this is the workflow that
                # actually works for Workday's country/state dropdowns — they
                # do NOT filter visually as you type, but Enter snaps to the
                # matching option). Verify the field afterwards.
                try:
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(750)
                except Exception:
                    pass
                fv = await _read_field_value()
                if fv and fv.lower() not in ("select one", "", term.lower()):
                    picked_txt = fv
                    logger.debug(f"  click_option: Enter selected {fv!r} after typing {term!r}")
                    break

                # ── Strategy 2: poll for options, then click best. If the best
                # VISIBLE match is weak (rank >= 2), the target may be further
                # down a long / non-filtering / virtualised list — scroll the
                # listbox and hunt for an exact/prefix match before clicking.
                cands = await _wait_for_options(max_ms=2500, poll_ms=250)
                best = None
                if cands:
                    cands.sort(key=lambda c: (c[0], c[1]))
                    best = cands[0]
                if best is None or best[0] >= 2:
                    hunted = await _scroll_hunt()
                    if hunted is not None and (best is None or hunted[0] < best[0]):
                        best = hunted
                if best:
                    _, _, o, txt = best
                    try:
                        await o.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    try:
                        await o.click(timeout=4000)
                        await page.wait_for_timeout(550)
                        fv = await _read_field_value()
                        if fv and fv.lower() not in ("select one", ""):
                            picked_txt = fv
                            logger.debug(f"  click_option: clicked option {txt!r} (verified={fv!r})")
                            break
                    except Exception:
                        pass

                # Neither Enter nor click landed — clear and try next term.
                logger.debug(f"  click_option: no commit after term {term!r}; trying next")
                await _clear_typed()
                await _close_dropdown()

            # VERIFY: field must show a non-empty value now. We check the
            # button's own text content + nearby read-only display element.
            field_now = ""
            try:
                field_now = ((await el.inner_text()) or "").strip()
            except Exception:
                pass
            if not field_now or field_now.lower() in ("select one", ""):
                # Try the inner input's value attribute.
                try:
                    field_now = (await el.evaluate(
                        "e => (e.value || (e.querySelector('input,select') "
                        "? (e.querySelector('input,select').value || '') : '')).toString()"
                    )) or ""
                except Exception:
                    pass

            if picked_txt and field_now and field_now.lower() != "select one":
                logger.info(f"  click_option: {label} = {picked_txt[:40]} (verified='{field_now[:40]}')")
                filled_selectors.add(selector)
                await page.wait_for_timeout(300)
                return True

            await _close_dropdown()
            logger.warning(f"  click_option: no match for '{value}' [{label}] "
                           f"(tried terms: {search_terms}, field after='{field_now}')")
            return False

        return True

    except Exception as e:
        logger.info(f"  Action error [{act}|{label}]: {e}")
        return False


async def run_actions(page, actions: list, resume_path: str,
                      on_stuck: Callable = None,
                      user_id: int = None) -> tuple[list, list]:
    """Run a list of actions. Returns (filled_labels, skipped_labels)."""
    filled  = []
    skipped = []
    filled_selectors: set = set()

    for action in actions:
        label = action.get("label") or action.get("selector") or action.get("action", "")
        ok = await dispatch_action(page, action, resume_path, on_stuck, filled_selectors, user_id)
        if ok:
            filled.append(label)
        else:
            if action.get("action") not in ("wait", "scroll_into_view", "hover", "press_key"):
                skipped.append(label)
        await page.wait_for_timeout(150)

    return filled, skipped


async def click_text_in_frames(page, texts: list, tags=("button", "a")) -> tuple[bool, str]:
    """Search all frames (main + iframes) for a VISIBLE element matching any text. Click it.
    Iterates all matches (not just .first) to skip hidden duplicates.
    Returns (success, matched_text)."""
    frames = page.frames  # includes main frame + all child frames
    for frame in frames:
        for text in texts:
            for tag in tags:
                try:
                    els = frame.locator(f"{tag}:has-text('{text}')")
                    count = await els.count()
                    for i in range(count):
                        el = els.nth(i)
                        if await el.is_visible():
                            await el.click(force=True, timeout=4000)
                            logger.info(f"  Frame click [{i}]: {tag}:has-text('{text}')")
                            return True, text
                except Exception:
                    continue
    return False, ""


async def upload_in_frames(page, resume_path: str) -> bool:
    """Search all frames for a file input and upload resume. Returns True on success."""
    frames = page.frames
    for frame in frames:
        for sel in ["input[type='file']", "input[accept*='pdf']", "input[accept*='.pdf']",
                    "#resume", "input[name*='resume' i]", "input[name*='cv' i]"]:
            try:
                el = frame.locator(sel).first
                if await el.count() > 0:
                    await el.set_input_files(resume_path)
                    logger.info(f"  Frame upload: {sel} [frame={frame.url[:60]}]")
                    return True
            except Exception:
                continue
    return False


def parse_gemini_json(text: str):
    """Strip markdown fences and parse JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)
