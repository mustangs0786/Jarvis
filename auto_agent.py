"""
auto_agent.py — Autonomous, checklist-driven browser agent for external portals
================================================================================
Per page the agent:

    dismiss overlays (cookie banners)
      → observe (screenshot with numbered marks + interactive elements)
      → PLAN the whole page in ONE LLM call → write todo.md
      → execute each item one-by-one, ticking it off in todo.md
      → upload resume if asked
      → advance to the next page (following new tabs)
      → repeat until submitted

It uses PROFILE + RESUME + .env creds + per-portal JOB_WIKI memory. When it can't
make progress after 5 tries it asks the user a pinpoint question with a screenshot.
Everything (fields, values, stuck points, questions) is logged to a real
runs/<domain>__<timestamp>/todo.md so any application is debuggable later.
"""

import os
import io
import json
import base64
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Callable

from dotenv import load_dotenv
load_dotenv()

from google.genai import types

from profile_manager import load_profile, learn_answer
from job_wiki import (get_portal_knowledge, save_portal_knowledge, domain_of,
                      portal_type, get_lessons, add_lessons)
from apply_skills.base import dispatch_action, parse_gemini_json, upload_in_frames
from apply_llm import llm_json, model_label
from workday import is_workday, is_workday_page, workday_prefill, workday_fill_dropdowns
from external_apply import ExternalApplyResult, is_submitted, _check_consent_boxes

logger = logging.getLogger(__name__)

MAX_ITERS   = 22          # page iterations (≈ one LLM plan call each)
STUCK_LIMIT = 3           # consecutive no-progress tries before asking the user
FLASH_MODEL = "gemini-3.5-flash"

# Persistent browser profile — stores cookies/sessions (NOT passwords) so a login
# done once is reused on every future run. Same idea as the LinkedIn cookie file.
# Overridable so parallel runs (e.g. a test harness next to the web app) don't
# fight over one Chrome profile — two launches on the same dir kill each other.
PROFILE_DIR = Path(os.getenv("APPLY_PROFILE_DIR", "browser_profile"))

# A page is an auth/login wall if its URL looks like one or it has a password box.
AUTH_URL_HINTS = ("login", "signin", "sign-in", "b2clogin", "/auth", "okta",
                  "/sso", "authenticate", "account/login", "accounts.")

EMAIL_TOKEN, PASS_TOKEN = "<EMAIL>", "<PASSWORD>"
SECRET_LABELS = ("password", "passwort", "pwd")

# Consent / cookie overlay buttons — curated so we don't accidentally hit form nav.
OVERLAY_TEXTS = [
    "Accept all cookies", "Accept All Cookies", "Accept all", "Accept All",
    "Accept Cookies", "Accept cookies", "Allow all cookies", "Allow all", "Allow All",
    "I Agree", "I agree", "Agree and continue", "Agree", "Got it", "Understood",
    "Reject all", "Reject All", "Decline", "No thanks", "Accept", "Close", "Dismiss",
]

# Marketing/AMA/newsletter popups that cover the form — no cookie text, just an
# X close button. Clicking these close buttons clears the overlay so the form
# underneath becomes interactable. Runs before every fill pass.
_CLOSE_BUTTON_JS = r"""
() => {
  let clicked = 0;
  const sels = [
    '[aria-label*="close" i]', '[aria-label*="dismiss" i]',
    'button[class*="close" i]', '[class*="modal"] [class*="close" i]',
    '[data-dismiss]', '[data-testid*="close" i]', '.modal-close', '.close-button',
  ];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      const visible = r.width > 0 && r.height > 0 && r.width < 120 && r.height < 120;
      if (visible && el.offsetParent !== null) {
        try { el.click(); clicked++; } catch (e) {}
        if (clicked >= 3) return clicked;
      }
    }
  }
  // Buttons/links whose entire visible text is an X glyph (×, ✕, ✖, X)
  for (const el of document.querySelectorAll('button, a, span[role="button"]')) {
    const t = (el.textContent || '').trim();
    if (['×','✕','✖','✗','x','X','╳'].includes(t)) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.width < 80 && el.offsetParent !== null) {
        try { el.click(); clicked++; } catch (e) {}
        if (clicked >= 3) return clicked;
      }
    }
  }
  return clicked;
}
"""


# ── Element collection (pierces shadow DOM; returns boxes for set-of-marks) ──
# Each element gets a deterministic `section_label` / `row_index` by walking
# up the DOM to the nearest "Work Experience N" / "Education N" header.
# This makes row identity unambiguous BEFORE any LLM call — see plan
# luminous-dazzling-turing.md for context.
_COLLECT_JS = r"""
(startIdx) => {
  const SEL = "input, textarea, select, button, a[href], [role=button], [role=checkbox], [role=radio], [contenteditable=true]";
  // Row-header pattern: "Work Experience 2", "Education 1", etc.
  const SECTION_RE = /(Work Experience|Employment|Education|Certifications?|Languages?|Projects?|Awards?)\s+(\d+)\b/i;
  // Un-numbered section header: a SHORT heading that is essentially just the
  // section word, e.g. "Work Experience", "Work Experience :", "Education".
  // Some ATSes (HPE) show ONE header over multiple un-numbered rows — we tag
  // the field with the bare word and let Python assign row indices by order.
  const SECTION_WORD_RE = /^(Work Experience|Employment|Education|Certifications?|Languages?|Projects?|Awards?)\b/i;
  function findSection(el) {
    let cursor = el;
    let unnum = null;   // nearest un-numbered header (fallback if no numbered one)
    for (let hops = 0; hops < 12 && cursor; hops++) {
      let prev = cursor.previousElementSibling;
      let scanned = 0;
      while (prev && scanned < 8) {
        // Check `prev` itself, plus any heading-like descendants.
        const cands = [prev];
        try {
          const hs = prev.querySelectorAll('h1,h2,h3,h4,h5,h6,legend,strong,b,[role=heading]');
          for (const h of hs) cands.push(h);
        } catch (e) {}
        for (const c of cands) {
          const raw = (c.innerText || c.textContent || '') + '';
          const txt = raw.replace(/\s+/g, ' ').trim();
          if (!txt || txt.length > 200) continue;
          const m = SECTION_RE.exec(txt);
          if (m) {
            const label = (m[1] + ' ' + m[2]).replace(/\s+/g, ' ').trim();
            return { label, idx: parseInt(m[2], 10) - 1 };
          }
          // Un-numbered: only accept a SHORT heading (<=40 chars) so we match a
          // real section title and not body text that mentions the word.
          if (!unnum && txt.length <= 40) {
            const u = SECTION_WORD_RE.exec(txt);
            if (u) unnum = { word: u[1].replace(/\s+/g, ' ').trim(), idx: -1 };
          }
        }
        prev = prev.previousElementSibling;
        scanned++;
      }
      cursor = cursor.parentElement;
    }
    return unnum;
  }
  const acc = [];
  function walk(root) {
    let m; try { m = root.querySelectorAll(SEL); } catch (e) { m = []; }
    for (const x of m) acc.push(x);
    let all; try { all = root.querySelectorAll('*'); } catch (e) { all = []; }
    for (const n of all) { if (n.shadowRoot) walk(n.shadowRoot); }
  }
  walk(document);

  const out = [];
  let idx = startIdx;
  for (const el of acc) {
    const tag  = (el.tagName || '').toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && type === 'hidden') continue;
    // NOTE: we deliberately do NOT skip inputs inside error containers here.
    // The convergence engine's whole job is to fix errored fields, so it must
    // be able to SEE them. The "Error - X" summary-panel LINKS are filtered
    // downstream by label (they aren't form inputs).
    const r = el.getBoundingClientRect();
    const visible = !!(el.offsetParent || el.getClientRects().length) && r.width > 1 && r.height > 1;
    if (!visible) continue;
    const root = el.getRootNode();
    const q1 = (sel) => { try { return root.querySelector ? root.querySelector(sel) : null; } catch (e) { return null; } };

    let label = el.getAttribute('aria-label') || '';
    if (!label) {
      const lb = el.getAttribute('aria-labelledby');
      if (lb) label = lb.split(/\s+/).map(id => { const n = (root.getElementById ? root.getElementById(id) : document.getElementById(id)); return n ? n.innerText : ''; }).join(' ');
    }
    if (!label && el.id) { const l = q1('label[for="' + CSS.escape(el.id) + '"]'); if (l) label = l.innerText; }
    if (!label) { const p = el.closest('label'); if (p) label = p.innerText; }
    if (!label) label = el.getAttribute('placeholder') || '';
    if (!label && (tag === 'button' || tag === 'a')) label = el.innerText || '';
    if (!label && (tag === 'input' || tag === 'textarea' || tag === 'select')) {
      let node = el, hops = 0;
      while (node && hops < 4 && !label) {
        let sib = node.previousElementSibling;
        while (sib && !label) {
          if (!/^(script|style|svg|input|button)$/i.test(sib.tagName)) {
            const t = (sib.innerText || '').replace(/\s+/g, ' ').trim();
            if (t && t.length <= 120) label = t;
          }
          sib = sib.previousElementSibling;
        }
        node = node.parentElement; hops++;
      }
    }
    if (!label) label = el.getAttribute('name') || '';
    label = (label || '').replace(/\s+/g, ' ').trim().slice(0, 160);

    // Section detection: deterministic row identity baked into the record.
    let section_label = '';
    let row_index     = null;
    let section_word  = '';
    try {
      const s = findSection(el);
      if (s) {
        if (s.idx >= 0) { section_label = s.label; row_index = s.idx; }
        else { section_word = s.word; }   // un-numbered: Python assigns the row #
      }
    } catch (e) {}

    // Viewport-relative coords so the red marks line up with the viewport screenshot.
    const rec = { idx, tag, type: type || null, label,
                  box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] };
    if (section_label) { rec.section_label = section_label; rec.row_index = row_index; }
    if (section_word) { rec.section_word = section_word; }
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      const v = (el.value || '').slice(0, 80); if (v) rec.value = v;
      // Capture name/id — a robust fallback when the visible label is mis-derived
      // (e.g. an intl-phone number input that picks up the adjacent "+91" as its
      // label). 'phone_number_field_number' still maps to the phone field.
      const nm = (el.getAttribute('name') || el.id || ''); if (nm) rec.name = nm.slice(0, 60);
    }

    // Widget type tells the executor HOW to fill: a typeahead/combobox needs
    // type-then-pick (the value won't "stick" with a plain .fill()), a native select
    // needs select_option. Without this the planner emits a bare "fill" and the
    // selection is never committed (the SAP "Skills" field failure).
    // NOTE: this MUST run before the chip-value extraction below, which is
    // gated on rec.widget === 'typeahead' — otherwise that block is dead code
    // and <input>-based typeaheads (Country Phone Code) look perpetually empty,
    // so the convergence engine re-opens & re-selects them every pass.
    {
      const role = (el.getAttribute('role') || '').toLowerCase();
      const ac   = (el.getAttribute('aria-autocomplete') || '').toLowerCase();
      const hp   = (el.getAttribute('aria-haspopup') || '').toLowerCase();
      // Also look for Workday's prompt markers. The outer button often has
      // data-automation-id mentioning "prompt" or "country" or "select"; the
      // INPUT inside may carry aria-haspopup. We promote on either signal.
      const aid  = (el.getAttribute('data-automation-id') || '').toLowerCase();
      const aex  = el.getAttribute('aria-expanded');
      let promptyAncestor = false;
      try {
        promptyAncestor = !!el.closest(
          '[data-automation-id*="prompt"], [data-automation-id*="Prompt"], '
        + '[data-automation-id*="promptOption"], [data-automation-id*="multiselectInputContainer"]'
        );
      } catch (e) {}
      if (tag === 'select') rec.widget = 'select';
      else if (role === 'combobox' || ac === 'list' || ac === 'both'
            || hp === 'listbox' || hp === 'menu' || hp === 'dialog'
            || aid.includes('prompt') || aid.includes('select-')
            || (aex !== null && tag === 'button')
            || promptyAncestor) rec.widget = 'typeahead';
    }

    // Typeahead / button widgets display their selected value as inner text
    // or in a child [data-automation-id="selectedItem"]. Without this Step A
    // never sees `(current: +91)` and keeps re-planning the same field every
    // iteration, which click_option then mangles by re-typing into the
    // already-selected dropdown's search input.
    if (!rec.value && tag === 'button') {
      let displayed = '';
      try {
        const valEl = el.querySelector(
          '[data-automation-id*="selectedItem"], [data-automation-id*="selectedValue"], '
        + '[class*="selectedItem"], [class*="selected-"], input'
        );
        if (valEl) {
          displayed = ((valEl.value !== undefined ? valEl.value : '') ||
                       valEl.innerText || valEl.textContent || '').trim();
        }
        if (!displayed) {
          displayed = (el.innerText || el.textContent || '')
                      .replace(/\s+/g, ' ').trim();
        }
      } catch (e) {}
      const placeholder = (el.getAttribute('placeholder') || '').trim().toLowerCase();
      const dl = displayed.toLowerCase();
      // Date pickers show "MM/YYYY" / "MM/DD/YYYY" placeholder text as their
      // innerText — that is NOT a filled value. If we capture it, the field
      // looks filled and gets skipped, so From/To never get populated.
      const isDatePh = (dl === 'mm/yyyy' || dl === 'mm / yyyy'
                        || dl === 'mm/dd/yyyy' || dl === 'dd/mm/yyyy'
                        || dl === 'yyyy' || /^(mm|dd|yyyy)\s*[\/\-.]/.test(dl));
      // Filter junk: very long button text (whole modal), placeholder, "select one".
      if (displayed && displayed.length <= 80
          && dl !== 'select one' && dl !== placeholder
          && !dl.startsWith('search') && !isDatePh
          && dl !== (label || '').toLowerCase()) {
        rec.value = displayed.slice(0, 80);
      }
    }
    // Workday <input>-based typeaheads (Country Phone Code, Skills) keep
    // their selected chips in a SIBLING element (not in el.value). Walk up
    // the container chain looking for chip-like descendants — without this
    // the field looks empty to Step A even when a chip is on screen.
    if (!rec.value && tag === 'input' && rec.widget === 'typeahead') {
      let chipText = '';
      try {
        let cursor = el;
        for (let h = 0; h < 4 && cursor && !chipText; h++) {
          const chips = cursor.querySelectorAll(
            '[data-automation-id*="selectedItem"], [data-automation-id*="selectedValue"], '
          + '[data-automation-id*="selectedListItem"], [class*="selectedItem"], '
          + '[class*="multiSelectChip"], [class*="selectedChip"]'
          );
          for (const ch of chips) {
            // Skip the X / remove button inside the chip — read just the text node(s).
            const t = (ch.innerText || ch.textContent || '')
                      .replace(/\s+/g, ' ')
                      .replace(/\s*[×xX]\s*$/, '')   // strip trailing remove glyph
                      .trim();
            if (t && t.length < 100 && t.toLowerCase() !== 'select one') {
              chipText = t;
              break;
            }
          }
          cursor = cursor.parentElement;
        }
      } catch (e) {}
      if (chipText) rec.value = chipText.slice(0, 80);
    }
    if (el.checked === true) rec.checked = true;
    if (el.getAttribute('required') !== null || el.getAttribute('aria-required') === 'true') rec.required = true;
    if (tag === 'select') rec.options = Array.from(el.options).map(o => (o.text || '').trim()).filter(Boolean).slice(0, 40);

    const ctrlRole = (el.getAttribute('role') || '').toLowerCase();
    const isRadio = (type === 'radio' || ctrlRole === 'radio');
    const isCheck = (type === 'checkbox' || ctrlRole === 'checkbox');
    if (isRadio || isCheck) {
      // Reliable control-kind signal regardless of <input> vs <div role=...>.
      rec.control = isRadio ? 'radio' : 'checkbox';
      // Custom (div) checkboxes/radios expose state via aria-checked, not .checked.
      const ac = (el.getAttribute('aria-checked') || '').toLowerCase();
      if (ac === 'true') rec.checked = true;
      let option = el.getAttribute('aria-label') || '';
      if (!option) { const l = el.closest('label'); if (l) option = l.innerText; }
      if (!option && el.id) { const l = q1('label[for="' + CSS.escape(el.id) + '"]'); if (l) option = l.innerText; }
      if (!option) { let s = el.nextSibling; while (s) { const t = (s.textContent || '').trim(); if (t) { option = t; break; } s = s.nextSibling; } }
      option = (option || '').replace(/\s+/g, ' ').trim().slice(0, 80);
      let q = '';
      const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) q = lg.innerText; }
      if (!q) { const grp = el.closest('[role=radiogroup],[role=group]'); if (grp) q = grp.getAttribute('aria-label') || ''; }
      if (!q) q = label;
      rec.option = option || label;
      rec.q = (q || '').replace(/\s+/g, ' ').trim().slice(0, 160);
    }

    el.setAttribute('data-agent-idx', String(idx));
    out.push(rec);
    idx++;
  }
  return { count: idx - startIdx, elements: out };
}
"""


async def collect_elements(page):
    elements, idx_frame, start = [], {}, 0
    for frame in page.frames:
        try:
            res = await frame.evaluate(_COLLECT_JS, start)
        except Exception:
            continue
        for rec in res.get("elements", []):
            elements.append(rec)
            idx_frame[rec["idx"]] = frame
        start += res.get("count", 0)
    return elements, idx_frame


def elements_to_text(elements: list, limit: int = 120) -> str:
    lines = []
    for e in elements[:limit]:
        parts = [f"[{e['idx']}]", e["tag"]]
        if e.get("type"):     parts.append(e["type"])
        if e.get("required"): parts.append("REQUIRED")
        if e.get("type") in ("radio", "checkbox"):
            if e.get("q"):             parts.append(f'Q:"{e["q"]}"')
            if e.get("section_label"): parts.append(f'({e["section_label"]})')
            parts.append(f'option:"{e.get("option", "")}"')
            parts.append("[SELECTED]" if e.get("checked") else "[ ]")
        else:
            if e.get("label"):         parts.append(f'"{e["label"]}"')
            if e.get("section_label"): parts.append(f'({e["section_label"]})')
            if e.get("widget"):        parts.append(f'<{e["widget"]}>')   # typeahead / select
            if e.get("value"):         parts.append(f'(current: {e["value"]})')
            if e.get("checked"):       parts.append("(checked)")
            if e.get("options"):       parts.append("options: " + " | ".join(e["options"]))
        lines.append(" ".join(parts))
    return "\n".join(lines) if lines else "(no interactive elements detected)"


def annotate_screenshot(png_bytes: bytes, elements: list) -> bytes:
    """Set-of-marks: draw each element's index on the screenshot for precise grounding."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return png_bytes
    try:
        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        d  = ImageDraw.Draw(im)
        W, H = im.size
        for e in elements:
            b = e.get("box")
            if not b:
                continue
            x, y, w, h = b
            # Skip off-viewport elements. Hidden skip-links (Workday's "Skip to
            # main content") sit at y < 0; without this guard, the label-bg
            # rectangle below ends up with y1 < y0 and PIL raises ValueError,
            # which the bare `except` swallows -> EVERY box silently dropped.
            if w <= 0 or h <= 0 or x + w < 0 or y + h < 0 or x > W or y > H:
                continue
            d.rectangle([x, y, x + w, y + h], outline=(214, 40, 40), width=2)
            tag = str(e["idx"])
            tw = 8 * len(tag) + 6
            # Clamp the label background so y1 >= y0 even when y is small.
            lbl_y0 = max(0, y - 15)
            lbl_y1 = max(lbl_y0 + 1, y)
            d.rectangle([x, lbl_y0, x + tw, lbl_y1], fill=(214, 40, 40))
            d.text((x + 3, max(0, y - 14)), tag, fill=(255, 255, 255))
        out = io.BytesIO(); im.save(out, format="PNG"); return out.getvalue()
    except Exception:
        return png_bytes


async def settle(page, quiet_ms: int = 500, timeout_ms: int = 4000):
    """Wait until the page stops changing before we read it — network goes idle AND
    the DOM stops mutating for `quiet_ms`. This is the settle-gate: it stops us from
    reasoning about a half-rendered page (the staleness race that broke dynamic forms)."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    try:
        await page.evaluate(
            """(quiet) => new Promise(resolve => {
                let timer = setTimeout(done, quiet);
                const obs = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(done, quiet); });
                obs.observe(document.documentElement, {childList:true, subtree:true, attributes:true});
                const hard = setTimeout(done, 4000);   // never wait forever
                function done(){ clearTimeout(hard); obs.disconnect(); resolve(); }
            })""", quiet_ms)
    except Exception:
        pass


async def observe(page, user_id: int, step: int, on_screenshot):
    # Read only once the page has settled (see settle()): otherwise the element
    # indices we hand the planner may already be stale by the time it answers.
    await settle(page)
    # Viewport screenshot (not full-page): the red numbered marks must line up with
    # what's on screen. Full-page + fixed modals/scrolled pages misaligned the boxes.
    raw = await page.screenshot(full_page=False)
    elements, idx_frame = await collect_elements(page)
    marked = annotate_screenshot(raw, elements)
    try:
        Path("output").mkdir(exist_ok=True)
        path = f"output/agent_{user_id}_step{step}.png"
        with open(path, "wb") as f:
            f.write(marked)
        if on_screenshot:
            await on_screenshot(path)
    except Exception:
        pass
    return base64.b64encode(marked).decode(), elements, idx_frame


async def push_shot(page, user_id: int, step: int, on_screenshot, tag: str = ""):
    """Push a quick, UN-annotated screenshot to keep the live view in sync.
    observe() only fires once per page (at the top of the loop, BEFORE filling),
    so the UI would otherwise freeze on the pre-fill state during fills/navigation.
    This is cheap (no element scan / marking) — used purely for the live preview."""
    if not on_screenshot:
        return
    try:
        raw = await page.screenshot(full_page=False)
        path = f"output/agent_{user_id}_step{step}{tag}.png"
        with open(path, "wb") as f:
            f.write(raw)
        await on_screenshot(path)
    except Exception:
        pass


# ── Overlay + tab helpers ───────────────────────────────────────────────────
async def dismiss_overlays(page) -> str:
    """Close cookie/consent overlays that block clicks. Returns text clicked, if any."""
    # Known consent-manager widgets first (text-independent). osano blocked
    # clicks on Glean's Greenhouse board in live testing.
    try:
        cm = page.locator(".osano-cm-accept-all, .osano-cm-accept, "
                          "button.osano-cm-dialog__close, #onetrust-accept-btn-handler, "
                          ".cky-btn-accept, button[aria-label='Accept all']").first
        if await cm.count() > 0 and await cm.is_visible():
            await cm.click(timeout=1500)
            await page.wait_for_timeout(400)
            return "consent-manager accept"
    except Exception:
        pass
    for frame in page.frames:
        for txt in OVERLAY_TEXTS:
            try:
                loc = frame.locator(
                    f"button:has-text('{txt}'), a:has-text('{txt}'), [role=button]:has-text('{txt}')")
                for i in range(min(await loc.count(), 2)):
                    el = loc.nth(i)
                    if await el.is_visible():
                        await el.click(timeout=1500)
                        await page.wait_for_timeout(400)
                        return txt
            except Exception:
                continue
    return ""


_BACKDROP_JS = r"""
() => {
  let removed = 0;
  // Stuck Bootstrap modal backdrops (e.g. SuccessFactors): fixed, full-viewport
  // dim layers left behind when a modal closes badly. They sit on top of the form
  // and swallow every click/keystroke — blocking the human during login handoff.
  document.querySelectorAll('.modal-backdrop').forEach(e => { e.remove(); removed++; });
  // body.modal-open locks scrolling — restore it so the user can scroll the form.
  if (document.body) {
    document.body.classList.remove('modal-open');
    document.body.style.overflow = '';
    document.body.style.position = '';
    document.body.style.paddingRight = '';
  }
  return removed;
}
"""

async def clear_blocking_overlays(page) -> int:
    """Remove stuck modal backdrops that block clicks AND scrolling. Returns count removed.
    Unlike dismiss_overlays (which clicks cookie/consent buttons), this strips leftover
    dim layers that have no button to click — the thing that froze the SuccessFactors
    and Maersk login pages for the user during handoff."""
    total = 0
    for frame in page.frames:
        try:
            total += await frame.evaluate(_BACKDROP_JS) or 0
        except Exception:
            continue
        try:  # close marketing/AMA/newsletter popups (X buttons) that hide the form
            total += await frame.evaluate(_CLOSE_BUTTON_JS) or 0
        except Exception:
            continue
    return total


_ERROR_JS = r"""
() => {
  const SEL = "[role=alert], [aria-invalid=true], .error, .error-message, .field-error, .invalid-feedback, .help-block, [class*='error' i], [class*='invalid' i]";
  const seen = new Set(), out = [];
  let nodes; try { nodes = document.querySelectorAll(SEL); } catch (e) { nodes = []; }
  for (const el of nodes) {
    if (!(el.offsetParent || el.getClientRects().length)) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t && t.length <= 120 && !seen.has(t)) { seen.add(t); out.push(t); }
  }
  return out.slice(0, 8);
}
"""

# Strings that the error selectors pick up but which are NOT errors — success
# banners ("draft saved successfully") and transient spinner text ("Loading...").
# Treating these as errors made the agent chase phantom problems and burn its retry
# budget instead of finding the real missing field (seen on SuccessFactors).
_NON_ERROR_HINTS = ("saved successfully", "successfully saved", "draft application was saved",
                    "loading", "please wait", "submitting", "processing")

def _is_real_error(text: str) -> bool:
    t = (text or "").strip().strip(".").lower()
    if not t:
        return False
    if t in ("success", "ok", "done", "...", "info"):
        return False
    return not any(h in t for h in _NON_ERROR_HINTS)

async def detect_validation_errors(page) -> list:
    """Collect visible form-validation error messages across frames (real errors only)."""
    out = []
    for frame in page.frames:
        try:
            for e in await frame.evaluate(_ERROR_JS):
                if _is_real_error(e) and e not in out:
                    out.append(e)
        except Exception:
            continue
    return out[:8]


# Forward/continue buttons we can click deterministically when the planner forgets
# to name one. Excludes the final "Submit" (that always needs user confirmation).
FORWARD_TEXTS = ["Create Account", "Save and Continue", "Save & Continue", "Continue",
                 "Next", "Save", "Sign Up", "Register", "Proceed", "Get Started"]

async def click_forward_button(page, on_notify=None) -> str:
    """Find and click the page's primary forward button. Returns its text, or ''.
    Waits for the button to become enabled (Workday enables it only after async
    validation), and force-clicks via JS if a normal click is intercepted."""
    for frame in page.frames:
        for txt in FORWARD_TEXTS:
            try:
                loc = frame.locator(
                    f"button:has-text('{txt}'), a:has-text('{txt}'), [role=button]:has-text('{txt}')")
                for i in range(min(await loc.count(), 3)):
                    el = loc.nth(i)
                    if not await el.is_visible():
                        continue
                    # Wait up to ~5s for it to enable (validation completing).
                    enabled = False
                    for _ in range(10):
                        try:
                            if await el.is_enabled():
                                enabled = True
                                break
                        except Exception:
                            break
                        await page.wait_for_timeout(500)
                    if not enabled:
                        if on_notify:
                            await on_notify(f"⚠️ '{txt}' is disabled — the form isn't valid yet "
                                            "(a required field is missing or the passwords don't match).")
                        continue
                    await el.scroll_into_view_if_needed()
                    try:
                        await el.click(timeout=4000)
                    except Exception:
                        # Custom React button / click intercepted → force a DOM click.
                        try:
                            await el.evaluate("e => e.click()")
                        except Exception:
                            continue
                    return txt
            except Exception:
                continue
    return ""


async def looks_like_auth(page) -> bool:
    """True if the page is a login/sign-up wall (URL hint or a visible password box)."""
    u = (page.url or "").lower()
    if any(h in u for h in AUTH_URL_HINTS):
        return True
    for frame in page.frames:
        try:
            if await frame.locator("input[type='password']:visible").count() > 0:
                return True
        except Exception:
            continue
    return False


async def try_auto_login(page, creds, on_notify=None) -> bool:
    """Attempt to log in with the .env credentials (APPLY_EMAIL / APPLY_PASSWORD).
    Returns True if it found a login form and submitted it (success is checked by the
    caller via looks_like_auth after settle), False if no login form was found.
    We only fill an existing Sign-In form — we never create an account automatically."""
    if not (creds.get("email") and creds.get("password")):
        return False
    # data-automation-id covers Workday, whose email input is type=text with no
    # email-ish name/id — the generic ladder missed it (Kyndryl live test).
    email_sel = ("input[data-automation-id='email'], input[data-automation-id='userName'], "
                 "input[type='email'], input[name*='email' i], input[id*='email' i], "
                 "input[autocomplete='username'], input[name*='user' i], input[id*='user' i], "
                 "input[name*='login' i], input[id*='login' i]")

    async def _find_pw():
        for frame in page.frames:
            try:
                pw = frame.locator("input[type='password']:visible").first
                if await pw.count() > 0:
                    return frame, pw
            except Exception:
                continue
        return None, None

    frame, pw = await _find_pw()

    # Many portals (e.g. SuccessFactors careers landing) show only a "Sign In" link —
    # the real email+password form appears after clicking it. If the email field isn't
    # reachable yet, click a Sign-In trigger to OPEN the form, then look again.
    em = frame.locator(email_sel).first if frame else None
    need_open = (frame is None) or (em is not None and await em.count() == 0)
    if need_open:
        for txt in ("Sign In", "Log In", "Member Login", "Login", "Sign in", "Log in"):
            try:
                trig = page.locator(
                    f"a:has-text('{txt}'), button:has-text('{txt}'), [role=button]:has-text('{txt}')").first
                if await trig.count() > 0 and await trig.is_visible():
                    await trig.click(timeout=4000)
                    await settle(page)
                    break
            except Exception:
                continue
        frame, pw = await _find_pw()

    if frame is None or pw is None:
        return False

    # Fill the EMAIL and verify it actually took — if we can't fill it, do NOT submit a
    # half form (that's what failed on SAP); bail so the user gets a clean handoff.
    em = frame.locator(email_sel).first
    email_ok = False
    try:
        if await em.count() > 0:
            await em.scroll_into_view_if_needed()
            await em.click()
            await em.fill(creds["email"], timeout=4000)
            email_ok = ((await em.input_value()) or "").strip() != ""
    except Exception:
        email_ok = False
    if not email_ok:
        if on_notify:
            await on_notify("⚠️ Couldn't fill the email field automatically — handing over to you.")
        return False

    try:
        await pw.fill(creds["password"], timeout=4000)
    except Exception:
        return False

    # Submit: Workday's stable automation-id first, then a named Sign-In button,
    # else press Enter in the password box.
    clicked = False
    try:
        wd_btn = frame.locator("[data-automation-id='signInSubmitButton'], "
                               "[data-automation-id='click_filter']").first
        if await wd_btn.count() > 0 and await wd_btn.is_visible():
            await wd_btn.click(timeout=4000); clicked = True
    except Exception:
        pass
    for txt in ([] if clicked else ("Sign In", "Log In", "Login", "Sign in", "Log in", "Continue", "Submit")):
        try:
            btn = frame.locator(
                f"button:has-text('{txt}'), input[type=submit][value*='{txt}' i], "
                f"[role=button]:has-text('{txt}')").first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=4000); clicked = True; break
        except Exception:
            continue
    if not clicked:
        try: await pw.press("Enter")
        except Exception: pass
    if on_notify:
        await on_notify("🔑 Trying to log in with your saved credentials…")
    return True


async def switch_if_new_tab(ctx, page):
    """If a click opened a new tab, switch to it."""
    try:
        pages = [p for p in ctx.pages if not p.is_closed()]
        if len(pages) > 1 and pages[-1] is not page:
            newp = pages[-1]
            try:
                await newp.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            return newp
    except Exception:
        pass
    return page


# ── todo.md run log ─────────────────────────────────────────────────────────
class TodoLog:
    def __init__(self, url: str):
        dom = domain_of(url) or "portal"
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = Path("runs") / f"{dom}__{ts}"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.dir = Path(".")
        self.path = self.dir / "todo.md"
        self.lines = [f"# Application — {url}", f"_started {datetime.now().isoformat()}_"]
        self._flush()

    def _flush(self):
        try:
            self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def page(self, n: int, ptype: str, url: str):
        self.lines += ["", f"## Page {n} — {ptype}", f"`{url}`"]
        self._flush()

    def item(self, status: str, label: str, detail: str = ""):
        mark = {"done": "[x]", "stuck": "[!]", "skip": "[~]", "ask": "[?]"}.get(status, "[ ]")
        self.lines.append(f"- {mark} {label}" + (f" — {detail}" if detail else ""))
        self._flush()

    def note(self, txt: str):
        self.lines.append(f"  > {txt}")
        self._flush()


# (old plan_page planner removed — external & Workday now use
#  apply_engine.converge_page via apply_orchestrator.run_application)


def _is_secret(label: str) -> bool:
    return any(s in (label or "").lower() for s in SECRET_LABELS)


async def execute_action(page, action, idx_frame, elements, resume_path, creds):
    act = (action.get("action") or "").lower()
    idx = action.get("index")
    val = action.get("value")
    if isinstance(val, str):
        creds = creds or {}
        val = (val.replace(EMAIL_TOKEN, creds.get("email", ""))
                  .replace(PASS_TOKEN, creds.get("password", "")))
    if idx is None or idx not in idx_frame:
        return False, f"element [{idx}] not found"
    selector = f'[data-agent-idx="{idx}"]'
    meta  = next((e for e in elements if e["idx"] == idx), {})
    label = meta.get("label") or meta.get("q") or selector
    # NEVER click Sign Out / Log Out — it destroys the session (and the whole filled
    # application). The scanned label alone is unreliable (Workday's logout is an
    # icon/menu item whose derived label isn't "log out"), so check ALL signals:
    # the scanned label, the planner's label, AND the element's LIVE text / aria-label
    # / href / title at click time. Holds across every tier (gateway, fills, vision).
    if act == "click":
        _sig = f"{meta.get('label','')} {action.get('label','')}"
        try:
            _live = await page.locator(selector).first.evaluate(
                "e => [e.innerText||'', e.getAttribute('aria-label')||'', "
                "e.getAttribute('href')||'', e.getAttribute('title')||'', "
                "e.getAttribute('data-automation-id')||''].join(' ')")
            _sig += " " + (_live or "")
        except Exception:
            pass
        _sig = _sig.lower()
        if any(p in _sig for p in ("sign out", "signout", "log out", "logout",
                                   "sign off", "log off", "logoff")):
            return False, f"refused: would log the user out ({(meta.get('label') or action.get('label') or 'element')[:40]})"
    # "check": the planner emits this for consent/agreement checkboxes — no value to
    # look up, just toggle by clicking. Map to "click" so the dispatcher handles it.
    dact = {"fill": "fill", "click": "click", "check": "click",
            "select": "click_option", "upload": "upload"}.get(act)
    # A typeahead/combobox or native <select> must be filled by type-then-pick — a plain
    # .fill() sets the text but never commits the selection (why SAP's "Skills" field
    # silently failed). Force those through click_option even if the planner said "fill".
    # We also force when the target is a <button> tag, because buttons can't be
    # filled — they're almost always Workday-style typeahead triggers and a plain
    # fill() is a silent no-op (Playwright doesn't error, field stays empty).
    if act == "fill" and (
        meta.get("widget") in ("typeahead", "select")
        or meta.get("tag") == "button"
        or (meta.get("options") and len(meta.get("options") or []) > 0)
    ):
        dact = "click_option"
    if not dact:
        return False, f"unknown action '{act}'"

    # Upload: the planner usually points at the visible "+"/drop-zone widget, but
    # set_input_files only works on the real (often hidden) <input type=file>. Find
    # that input directly across frames. If none exists yet, click the widget to
    # reveal it (some portals inject the input on click), then retry.
    if act == "upload":
        if await upload_in_frames(page, resume_path):
            return True, f"upload [{idx}] {label} → hidden file input"
        try:
            await page.locator(selector).first.click(timeout=2000)
            await page.wait_for_timeout(900)
        except Exception:
            pass
        if await upload_in_frames(page, resume_path):
            return True, f"upload [{idx}] {label} → file input after click"
        return False, f"upload [{idx}] {label} — no <input type=file> found"

    ok = await dispatch_action(
        page, {"action": dact, "selector": selector, "value": val, "label": label}, resume_path)
    return ok, f"{act} [{idx}] {label}"
