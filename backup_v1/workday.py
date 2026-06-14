"""
workday.py — Deterministic field handling for Workday portals (*.myworkdayjobs.com)
====================================================================================
Workday tags every field with a stable `data-automation-id` that is identical
across all tenants. So for the standard fields we fill by exact selector — fast,
deterministic, no LLM guessing, no "DOM sparse" timing flakiness.

Anything NOT in the map (company-specific questions, fields we don't have data
for) is left to the generic LLM agent in auto_agent — graceful fallback.

This is integrated into auto_agent's loop (not a separate engine), so it reuses
the existing persistent session, login handoff, dropdown handling, todo log, and
submit confirmation.
"""

import logging
from apply_skills.base import dispatch_action

logger = logging.getLogger(__name__)

# Stable Workday navigation selectors (same on every tenant).
WORKDAY_NEXT_BUTTON   = "[data-automation-id='pageFooterNextButton']"   # "Save and Continue"
WORKDAY_SUBMIT_BUTTON = "[data-automation-id='bottom-navigation-next-button']"
WORKDAY_COOKIE_ACCEPT = "[data-automation-id='legalNoticeAcceptButton']"


def is_workday(url: str) -> bool:
    return "myworkdayjobs.com" in (url or "").lower()


async def is_workday_page(page) -> bool:
    """Detect Workday by FINGERPRINT, not just URL — many companies (Blue Yonder,
    etc.) run Workday on a vanity domain. Workday tags everything with
    data-automation-id, so its presence is a reliable signal."""
    if is_workday(page.url):
        return True
    try:
        sig = await page.locator(
            "[data-automation-id='pageFooterNextButton'], "
            "[data-automation-id='legalNameSection_firstName'], "
            "[data-automation-id='legalNoticeAcceptButton']").count()
        if sig > 0:
            return True
        if await page.locator("[data-automation-id]").count() >= 10:
            return True
    except Exception:
        pass
    return False


# Standard Workday field id → profile key. Only fields we reliably know; the rest
# (address line, postal code, source dropdown, questions, disclosures) fall through
# to the LLM agent. Each is tried as both the input itself and an input within it.
_FIELDS = [
    ("legalNameSection_firstName", "first_name"),
    ("legalNameSection_lastName",  "last_name"),
    ("addressSection_city",        "city"),
    ("phone-number",               "phone"),
]


async def workday_prefill(page, profile: dict, on_notify=None) -> list:
    """Fill the standard Workday fields we know, by exact data-automation-id.
    Returns the list of field ids filled. Missing/unknown fields are left for
    the LLM agent to handle."""
    filled = []
    for aid, key in _FIELDS:
        val = str(profile.get(key) or "").strip()
        if not val:
            continue
        sel = f"input[data-automation-id='{aid}'], [data-automation-id='{aid}'] input"
        try:
            ok = await dispatch_action(
                page, {"action": "fill", "selector": sel, "value": val, "label": aid}, "")
            if ok:
                filled.append(aid)
        except Exception:
            continue
    if filled:
        logger.info(f"  Workday prefill: {filled}")
        if on_notify:
            await on_notify(f"⚡ Workday: filled {', '.join(filled)} by exact selector.")
    return filled


# Standard Workday dropdowns → default value. Filled ONLY when still empty
# ("Select One"), so they don't re-toggle every iteration. Company-specific
# dropdowns (e.g. "<Company> Source") vary, so they're left to the LLM/user.
_DROPDOWNS = [
    ("phone-device-type",  "Mobile"),
    ("country-phone-code", "India"),     # substring-matches "India (+91)"
]

async def workday_fill_dropdowns(page, profile: dict, on_notify=None) -> list:
    """Set the standard Workday dropdowns (country, phone type/code) when empty."""
    country = str(profile.get("country") or "India").strip()
    items = [("countryDropdown", country), ("country", country)] + _DROPDOWNS
    filled = []
    for aid, val in items:
        sel = f"[data-automation-id='{aid}']"
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0 or not await loc.is_visible():
                continue
            cur = ((await loc.inner_text()) or "").strip().lower()
            # Guard: only fill if still the placeholder (don't re-toggle a set value).
            if cur and "select one" not in cur and cur not in ("select", "select...", ""):
                continue
            ok = await dispatch_action(
                page, {"action": "click_option", "selector": sel, "value": val, "label": aid}, "")
            if ok:
                filled.append(f"{aid}={val}")
        except Exception:
            continue
    if filled:
        logger.info(f"  Workday dropdowns: {filled}")
        if on_notify:
            await on_notify(f"⚡ Workday dropdowns: {', '.join(filled)}")
    return filled
