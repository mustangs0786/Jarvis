"""
apply_vision.py — vision tiers for the apply engine
===================================================
Two LLM-vision capabilities that sit ON TOP of the deterministic convergence
engine (apply_engine.converge_page):

  vision_audit(page, profile)   — QA a half-filled page against the profile and
      report values that filled but are semantically WRONG (e.g. Country shows
      Pakistan but the profile says India), required fields still empty, and any
      inline errors the DOM scan missed. The page's OWN validation never catches
      a valid-but-wrong value — only vision can.

  vision_recover(page, profile, errors) — last-resort stuck recovery: when the
      deterministic engine can't advance, look at the screenshot + errors and
      propose ONE corrective action. Bounded, confidence-gated, NEVER submits.

Validated as a read-only notebook prototype first (debug_agent.ipynb cell 9b):
silent on a correct page, precise on a wrong one.
"""
import os
import re
import base64

from auto_agent import (collect_elements, annotate_screenshot, execute_action,
                        elements_to_text, settle, FLASH_MODEL)
from apply_engine import _profile_ctx, _assign_section_rows, classify_field
from apply_llm import llm_json


def _norm_lbl(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("*", "")).strip()


def _norm(s):
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _creds():
    return {"email": os.getenv("APPLY_EMAIL", ""), "password": os.getenv("APPLY_PASSWORD", "")}


async def _full_marked_shot(page):
    """Scroll to top, re-scan (so boxes align with the document), number un-numbered
    rows, full-page screenshot with red boxes. Returns (elements, idx_frame, png)."""
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    await page.wait_for_timeout(300)
    elems, idx_fr = await collect_elements(page)
    _assign_section_rows(elems)
    try:
        raw = await page.screenshot(full_page=True)
    except Exception:
        raw = await page.screenshot()
    return elems, idx_fr, annotate_screenshot(raw, elems)


# ── Tier A: correctness audit ────────────────────────────────────────────────
_AUDIT_PROMPT = """You are QA-checking a half-filled job-application page before submission.
Red numbered boxes mark interactive fields (the number is each field's index).

CANDIDATE PROFILE (ground truth):
{pctx}

Report ONLY genuine problems. MOST FIELDS WILL BE CORRECT — empty arrays are the
normal, expected result. Bias HARD toward "ok": when unsure, do NOT report a field.

Return STRICT JSON:
{{
  "ok": <true if no real problems>,
  "wrong":   [{{"index": <int>, "field": "<label>", "shown": "<current value>", "should_be": "<correct value from profile>", "why": "<short>"}}],
  "missing": [{{"index": <int>, "field": "<label>", "should_be": "<value if known, else empty>"}}],
  "errors":  ["<inline red error text visible on the page>"]
}}

WRONG — include a field ONLY if its visible value CONTRADICTS the profile in MEANING
(Country shows 'Pakistan' but profile says India; misspelled name; wrong date; a
dropdown snapped to the wrong option). NOT wrong (never report): same value in
different casing/spacing/punctuation; a formatting variant ('India (+91)' == India;
'12/2022' == Dec 2022); an acceptable synonym; a value simply not in the profile.
Before adding a field, confirm shown and should_be mean DIFFERENT things; if equal
or equivalent, OMIT it. Focus on dropdowns / typeaheads / dates.

MISSING — a field with a clear visible label, marked required (*), that is empty or
shows a placeholder ('Please Select'). Do NOT invent fields; never output 'Unknown
required field'; the resume-upload area is not a missing field.

ERRORS — copy any inline red error text actually visible.

Refer to fields by their red index number. Return ONLY the JSON object."""


async def vision_audit(page, profile, gemini_client=None):
    """Full-page screenshot vs profile. Returns {ok, wrong[], missing[], errors[]}
    with false-positive 'wrong' (shown == should_be after normalize) filtered out."""
    elems, idx_fr, marked = await _full_marked_shot(page)
    out = llm_json(_AUDIT_PROMPT.format(pctx=_profile_ctx(profile)),
                   image_b64=base64.b64encode(marked).decode(),
                   gemini_client=gemini_client, gemini_model=FLASH_MODEL) or {}
    wrong = [w for w in (out.get("wrong") or [])
             if w.get("should_be") and _norm(w.get("shown")) != _norm(w.get("should_be"))]
    return {
        "ok":      bool(out.get("ok")) and not wrong,
        "wrong":   wrong,
        "missing": out.get("missing") or [],
        "errors":  out.get("errors") or [],
        # The EXACT scan the LLM saw — correct_from_audit MUST reuse these so the
        # reported indices line up with the right elements (re-scanning reassigns
        # indices and writes values into the WRONG fields).
        "_elements":  elems,
        "_idx_frame": idx_fr,
    }


async def correct_from_audit(page, profile, audit, *, on_notify=None):
    """Clear + refill ONLY the confidently-wrong (and missing-with-value) fields the
    audit named — using the SAME scan vision_audit saw (so indices line up), and
    only when the element at that index still matches the label the audit named
    (so a stale/hallucinated index can't corrupt an unrelated field). Never clicks
    Submit. Returns the count of fields corrected."""
    targets = [(w.get("index"), w.get("should_be"), w.get("field"))
               for w in audit.get("wrong", []) if w.get("should_be")]
    targets += [(m.get("index"), m.get("should_be"), m.get("field"))
                for m in audit.get("missing", []) if m.get("should_be")]
    if not targets:
        return 0
    creds = _creds()
    elems = audit.get("_elements")
    idx_fr = audit.get("_idx_frame")
    if not elems:                      # fallback only (shouldn't happen)
        elems, idx_fr = await collect_elements(page)
        _assign_section_rows(elems)
    by_idx = {e.get("idx"): e for e in elems}
    fixed = 0
    for idx, value, field_label in targets:
        e = by_idx.get(idx)
        if e is None or not value or classify_field(e) == "skip":
            continue
        # SAFETY: the element at this index must match the label the audit named.
        # If it doesn't, the indices have drifted — skip rather than write the
        # value into the wrong field (this was the name-in-salary corruption).
        el_lab, fl = _norm_lbl(e.get("label")), _norm_lbl(field_label)
        if fl and el_lab and not (el_lab == fl or el_lab in fl or fl in el_lab):
            print(f"  vision skip: [{idx}] is '{e.get('label','')[:24]}', "
                  f"not '{field_label[:24]}' (index drift)")
            continue
        cur = _norm(e.get("value"))
        want = _norm(value)
        if cur and want and (cur == want or want in cur or cur in want):
            print(f"  vision skip: [{idx}] {field_label[:30]} already shows correct value")
            continue
        is_choice = (e.get("tag") == "select" or e.get("widget") in ("select", "typeahead")
                     or bool(e.get("options")))
        act = "select" if is_choice else "fill"
        try:
            ok, note = await execute_action(page, {"action": act, "index": idx,
                                                   "value": value, "label": e.get("label", "")},
                                            idx_fr, elems, "", creds)
            if ok:
                fixed += 1
                if on_notify:
                    await on_notify(f"🔧 Vision fix: {e.get('label','field')[:30]} → {value}")
        except Exception:
            pass
        await page.wait_for_timeout(150)
    return fixed


# ── Tier B: last-resort stuck recovery ───────────────────────────────────────
_RECOVER_PROMPT = """A job-application page will NOT advance. Red numbered boxes mark
interactive fields (the number is each field's index).

CANDIDATE PROFILE:
{pctx}

VALIDATION ERRORS / WHY IT IS STUCK:
{errors}

ELEMENTS:
{elements}

Propose the SINGLE most useful action to get past the block. Return STRICT JSON:
{{"index": <int or null>, "action": "<fill|select|check|click>", "value": "<value or empty>", "reason": "<short>"}}

RULES
- Use ONLY data derivable from the profile for `value`. If you can't derive it, return index=null.
- NEVER click Submit / Submit Application / Sign Out / Log Out / Create Account / Delete.
- Prefer fixing a required field the errors point to (fill / select / check).
- Refer to the field by its red index number. Return ONLY the JSON object."""

_FORBIDDEN_CLICK = ("submit", "sign out", "log out", "logout", "create account",
                    "delete account", "delete", "sign off")


async def vision_recover(page, profile, errors, gemini_client=None, *, on_notify=None, max_tries=2):
    """Last resort: look at the page + errors, propose ONE corrective action, do it.
    Bounded, confidence-gated, NEVER submits / signs out. Returns actions taken."""
    creds = _creds()
    err_text = "\n".join(f"- {e}" for e in (errors or [])[:8]) or "(page did not advance; no explicit error text)"
    taken = 0

    # RC3: error-FIRST. The site already flags exactly which fields are wrong
    # (red outline + "X is required"). Fill those by their own index straight
    # from the profile — deterministic, no vision guess — before falling back
    # to the LLM (which otherwise fixates on a salient widget like a Country
    # dropdown instead of the actually-empty required fields).
    from apply_engine import scan_page_errors, resolve_field_value
    from auto_agent import collect_elements
    try:
        elems0, idxf0 = await collect_elements(page)
        errs0 = await scan_page_errors(page)
        err_idxs = {e.get("idx") for e in errs0 if e.get("idx") is not None}
        for e in elems0:
            if e.get("idx") in err_idxs:
                val = resolve_field_value(e, profile)
                if not val:
                    continue
                try:
                    ok, _n = await execute_action(page, {"action": "fill", "index": e.get("idx"),
                                "value": val, "label": e.get("label", "")}, idxf0, elems0, "", creds)
                    if ok:
                        taken += 1
                        if on_notify:
                            await on_notify(f"🎯 Fixed errored field: {e.get('label','')[:40]}")
                        await settle(page)
                except Exception:
                    pass
        if taken:
            return taken   # errored fields handled deterministically; skip the guess
    except Exception:
        pass

    for _ in range(max_tries):
        elems, idx_fr, marked = await _full_marked_shot(page)
        out = llm_json(_RECOVER_PROMPT.format(pctx=_profile_ctx(profile), errors=err_text,
                                              elements=elements_to_text(elems)),
                       image_b64=base64.b64encode(marked).decode(),
                       gemini_client=gemini_client, gemini_model=FLASH_MODEL) or {}
        idx = out.get("index")
        if idx is None:
            break
        action = (out.get("action") or "fill").lower()
        value = out.get("value") or ""
        e = next((x for x in elems if x.get("idx") == idx), {})
        lab = (e.get("label") or "").lower()
        if action == "click" and any(f in lab for f in _FORBIDDEN_CLICK):
            break   # guardrail: never let vision submit / sign out
        if action in ("fill", "select", "check") and not value:
            break
        try:
            ok, note = await execute_action(page, {"action": action, "index": idx,
                                                   "value": value, "label": e.get("label", "")},
                                            idx_fr, elems, "", creds)
            if on_notify:
                await on_notify(f"👁️ Vision recovery: {note}")
            if ok:
                taken += 1
            await settle(page)
        except Exception:
            break
    return taken
