"""
linkedin_url_extractor.py
=========================
Detects whether a LinkedIn job is Easy Apply or external apply.
If external, captures the company portal URL by clicking the Apply button.

Usage:
    result = await detect_easy_apply("https://www.linkedin.com/jobs/view/...")
    # {"easy_apply": True/False, "apply_url": "...", "portal": "...", "error": "..."}

CLI login:
    python linkedin_url_extractor.py login
"""

import os
import re
import json
import asyncio
import logging
from pathlib import Path
from typing import Callable
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── Portal patterns ───────────────────────────────────────────────────────────

PORTAL_PATTERNS = {
    "greenhouse":      ["greenhouse.io", "boards.greenhouse.io"],
    "lever":           ["lever.co", "jobs.lever.co"],
    "ashby":           ["ashbyhq.com", "jobs.ashbyhq.com"],
    "workday":         ["myworkdayjobs.com", "workday.com"],
    "smartrecruiters": ["smartrecruiters.com"],
    "icims":           ["icims.com"],
    "taleo":           ["taleo.net"],
    "successfactors":  ["successfactors.com", "sapsf.com"],
    "jobvite":         ["jobvite.com"],
    "bamboohr":        ["bamboohr.com"],
    "oraclecloud":     ["oraclecloud.com", "fa.oraclecloud.com"],
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.linkedin.com/jobs/search/",
}


def identify_portal(url: str) -> str:
    u = url.lower()
    for portal, patterns in PORTAL_PATTERNS.items():
        if any(p in u for p in patterns):
            return portal
    return "custom"


# ── Cookie management ─────────────────────────────────────────────────────────

COOKIES_FILE = Path("linkedin_cookies.json")


def save_cookies(cookies: list):
    COOKIES_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    logger.info(f"  Cookies saved → {COOKIES_FILE}")


def load_cookies() -> list:
    if not COOKIES_FILE.exists():
        return []
    try:
        return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def cookies_exist() -> bool:
    return COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 100


def do_manual_login() -> bool:
    """
    Open a visible browser at LinkedIn login, wait for the user to log in manually,
    then save cookies. Called via asyncio.to_thread so must be synchronous.
    Returns True when cookies are saved, False on timeout.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, slow_mo=80,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx  = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = ctx.new_page()
        try:
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            # Wait up to 3 minutes for user to finish logging in
            for _ in range(36):
                if any(k in page.url for k in ("feed", "mynetwork", "jobs", "checkpoint/post-login")):
                    save_cookies(ctx.cookies())
                    browser.close()
                    return True
                page.wait_for_timeout(5000)
            browser.close()
            return False
        except Exception as e:
            logger.error(f"Manual login failed: {e}")
            try:
                browser.close()
            except Exception:
                pass
            return False


def do_login(email: str = "", password: str = "") -> bool:
    """Robust one-time LinkedIn sign-in that SAVES the session for reuse.

    Opens a visible browser, best-effort auto-fills LINKEDIN_EMAIL/PASSWORD, then waits
    up to 3 minutes for the sign-in to complete — so you can solve a CAPTCHA / 2FA or
    finish by hand if auto-fill misses. Saves cookies (linkedin_cookies.json) on success.
    Run once (UI 'Connect LinkedIn' button, or `python linkedin_url_extractor.py login`)
    and every later run reuses the session. Sync (for asyncio.to_thread)."""
    from playwright.sync_api import sync_playwright

    EMAIL_SELS = ["#username", "input[name='session_key']", "input[autocomplete='username']",
                  "input[type='email']", "input[type='text']"]
    PWD_SELS   = ["#password", "input[name='session_password']",
                  "input[autocomplete='current-password']", "input[type='password']"]

    def _fill(page, sels, val):
        for s in sels:
            try:
                loc = page.locator(s).first
                if loc.count() and loc.is_visible():
                    loc.fill(val); return True
            except Exception:
                continue
        return False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=80,
                                    args=["--disable-blink-features=AutomationControlled"])
        ctx  = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = ctx.new_page()
        try:
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            for sel in ("button[action-type='ACCEPT']", "button:has-text('Accept')",
                        "button:has-text('Agree')"):
                try:
                    b = page.locator(sel).first
                    if b.count() and b.is_visible():
                        b.click(timeout=2000); break
                except Exception:
                    continue
            if email and password and _fill(page, EMAIL_SELS, email):
                _fill(page, PWD_SELS, password)
                for sel in ("button[type='submit']", "button[aria-label='Sign in']",
                            "button:has-text('Sign in')"):
                    try:
                        b = page.locator(sel).first
                        if b.count() and b.is_visible():
                            b.click(); break
                    except Exception:
                        continue
            # Wait for login to land (auto or manual) — solve CAPTCHA/2FA in the window.
            for _ in range(36):
                if any(k in page.url for k in ("feed", "mynetwork", "jobs", "checkpoint/post-login")):
                    save_cookies(ctx.cookies())
                    browser.close()
                    return True
                page.wait_for_timeout(5000)
            browser.close()
            return False
        except Exception as e:
            logger.error(f"do_login failed: {e}")
            try: browser.close()
            except Exception: pass
            return False


async def _fill_first(page, selectors, value, timeout=4000) -> bool:
    """Fill the first selector that becomes visible. Returns True on success."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.fill(value)
            return True
        except Exception:
            continue
    return False


async def _linkedin_login(page, email: str, password: str) -> bool:
    """Best-effort auto-fill of the LinkedIn sign-in form. Returns True if it filled +
    submitted, False if the fields couldn't be found (so the caller can fall back to a
    MANUAL login in the open browser). Never raises — LinkedIn changes its login UI and
    shows CAPTCHAs, so we degrade to human completion instead of crashing the run."""
    await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    # Dismiss the cookie-consent banner if it's covering the form.
    for sel in ("button[action-type='ACCEPT']", "button:has-text('Accept')",
                "button:has-text('Agree')", "button:has-text('Allow')"):
        try:
            b = page.locator(sel).first
            if await b.count() and await b.is_visible():
                await b.click(timeout=2000)
                break
        except Exception:
            continue

    # Email / phone — '#username' is the stable id on the current login page;
    # 'session_key' is the classic name. Try several so we survive UI changes.
    if not await _fill_first(page, ["#username", "input[name='session_key']",
                                    "input[autocomplete='username']",
                                    "input[type='email']", "input[type='text']"], email, timeout=6000):
        return False
    await _fill_first(page, ["#password", "input[name='session_password']",
                             "input[autocomplete='current-password']",
                             "input[type='password']"], password)

    for sel in ("button[type='submit']", "button[aria-label='Sign in']",
                "button:has-text('Sign in')"):
        try:
            b = page.locator(sel).first
            if await b.count() and await b.is_visible():
                await b.click(timeout=4000)
                break
        except Exception:
            continue
    await page.wait_for_timeout(4000)
    return True


async def login_and_save_cookies(email: str, password: str) -> bool:
    """Open LinkedIn login in a visible browser, save cookies on success."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=100,
                                          args=["--disable-blink-features=AutomationControlled"])
        ctx  = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await ctx.new_page()
        try:
            await _linkedin_login(page, email, password)

            for _ in range(36):  # wait up to 3 min for CAPTCHA/OTP
                if any(k in page.url for k in ["feed", "mynetwork", "jobs"]):
                    save_cookies(await ctx.cookies())
                    await browser.close()
                    return True
                await asyncio.sleep(5)

            await browser.close()
            return False
        except Exception as e:
            logger.error(f"Login failed: {e}")
            await browser.close()
            return False


# ── Easy Apply detector ───────────────────────────────────────────────────────

async def detect_easy_apply(job_url: str, debug: bool = False) -> dict:
    """
    1. Login with cookies (or .env creds if no cookies)
    2. Open the job URL
    3. Read button text — if 'Easy Apply' → done, no click
    4. Otherwise click Apply and capture the redirect URL
    """
    from playwright.async_api import async_playwright

    cookies  = load_cookies()
    email    = os.getenv("LINKEDIN_EMAIL", "").strip()
    password = os.getenv("LINKEDIN_PASSWORD", "").strip()

    result = {"easy_apply": False, "apply_url": "", "portal": "", "error": ""}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx  = await browser.new_context(viewport={"width": 1280, "height": 900},
                                         user_agent=HEADERS["User-Agent"])
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await ctx.new_page()

        try:
            # ── Login ─────────────────────────────────────────────────────
            if cookies:
                await ctx.add_cookies(cookies)
                logger.info(f"  Loaded {len(cookies)} cookies")
            else:
                # No saved session → do NOT attempt a fragile inline login here (that
                # lands on the sign-in page and gets scraped as the "job"). The session
                # must be established first via the 'Connect LinkedIn' button / CLI.
                await browser.close()
                result["error"] = (
                    "LinkedIn not connected. Click 'Connect LinkedIn' first (or run "
                    "`uv run python linkedin_url_extractor.py login`) — it saves your "
                    "session, then retry this URL."
                )
                return result

            # ── Open job URL ──────────────────────────────────────────────
            logger.info(f"  Loading: {job_url}")
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # ── Find Apply button (scoped to job details pane) ────────────
            apply_el = None
            APPLY_SELS = [
                "button:has-text('Easy Apply')",
                "a:has-text('Easy Apply')",
                "button:has-text('Apply')",
                "a:has-text('Apply')",
            ]
            SCOPE_SELS = [
                ".jobs-unified-top-card",
                ".jobs-s-apply",
                ".job-view-layout",
                ".jobs-details",
                "main",
            ]

            # Pass 1: scoped search (avoids sidebar job cards)
            for scope_sel in SCOPE_SELS:
                scope = page.locator(scope_sel).first
                try:
                    if not await scope.count():
                        continue
                except Exception:
                    continue
                for sel in APPLY_SELS:
                    try:
                        el = scope.locator(sel).first
                        if await el.count() > 0 and await el.is_visible():
                            txt = (await el.inner_text()).strip()
                            if len(txt) < 60:
                                apply_el = el
                                logger.info(f"  Found in {scope_sel}: {sel}")
                                break
                    except Exception:
                        continue
                if apply_el:
                    break

            # Pass 2: page-wide fallback with short-text filter
            if not apply_el:
                for sel in APPLY_SELS:
                    try:
                        els = page.locator(sel)
                        for i in range(min(await els.count(), 10)):
                            el = els.nth(i)
                            if not await el.is_visible():
                                continue
                            txt = (await el.inner_text()).strip()
                            if len(txt) < 60:
                                apply_el = el
                                logger.info(f"  Fallback: {sel}")
                                break
                    except Exception:
                        continue
                    if apply_el:
                        break

            if not apply_el:
                result["error"] = "No Apply button found"
                return result

            # ── Check button text ─────────────────────────────────────────
            btn_text = (await apply_el.inner_text()).strip().lower()
            if debug:
                print(f"  [DEBUG] Button text: '{btn_text}'")

            if "easy apply" in btn_text:
                result["easy_apply"] = True
                logger.info("  ⚡ Easy Apply — no click needed")
            else:
                # ── Try href extraction first (no click needed) ───────────
                from urllib.parse import urlparse, parse_qs, unquote
                external_url = ""
                try:
                    tag  = (await apply_el.evaluate("el => el.tagName")).lower()
                    href = (await apply_el.get_attribute("href") or "").strip()
                    if debug:
                        print(f"  [DEBUG] tag={tag} href={href[:120]}")
                    if tag == "a" and href:
                        if href.startswith("http") and "linkedin.com" not in href:
                            # Direct external URL
                            external_url = href
                            logger.info(f"  🌐 Direct href: {href[:80]}")
                        elif "linkedin.com/safety/go" in href or "linkedin.com/redir" in href:
                            # LinkedIn safety/redirect wrapper — extract `url` param
                            qs = parse_qs(urlparse(href).query)
                            redir = unquote(qs.get("url", [""])[0])
                            if redir and "linkedin.com" not in redir:
                                external_url = redir
                                logger.info(f"  🌐 Safety redirect → {redir[:80]}")
                except Exception as _he:
                    logger.debug(f"  href parse failed: {_he}")

                if external_url:
                    result["apply_url"] = external_url
                    result["portal"]    = identify_portal(external_url)
                else:
                    # ── Fallback: click and capture new tab / navigation ──
                    logger.info("  Clicking Apply (no href)...")
                    await apply_el.click()
                    max_wait_ms = 15000
                    poll_ms     = 500
                    waited      = 0
                    while waited < max_wait_ms:
                        await page.wait_for_timeout(poll_ms)
                        waited += poll_ms

                        # Current tab navigated to external site?
                        cur_url = page.url
                        if "linkedin.com" not in cur_url and cur_url not in ("about:blank", ""):
                            external_url = cur_url
                            if debug:
                                print(f"  [DEBUG] Tab navigated ({waited}ms): {external_url}")
                            break

                        # New tab opened?
                        for pg in ctx.pages:
                            if pg == page:
                                continue
                            tab_url = pg.url
                            if tab_url in ("about:blank", ""):
                                continue
                            if "linkedin.com" in tab_url:
                                try:
                                    await pg.wait_for_url(
                                        re.compile(r"^(?!.*linkedin\.com)"), timeout=6000
                                    )
                                    tab_url = pg.url
                                except Exception:
                                    continue
                            if "linkedin.com" not in tab_url and tab_url not in ("about:blank", ""):
                                try:
                                    await pg.wait_for_load_state("domcontentloaded", timeout=5000)
                                except Exception:
                                    pass
                                external_url = pg.url
                                if debug:
                                    print(f"  [DEBUG] New tab ({waited}ms): {external_url}")
                                await pg.close()
                                break
                        if external_url:
                            break

                    if external_url and "linkedin.com" not in external_url:
                        result["apply_url"] = external_url
                        result["portal"]    = identify_portal(external_url)
                        logger.info(f"  🌐 External: {external_url}")
                    else:
                        # Check if an Easy Apply modal opened
                        modal_open = False
                        for sel in ["[data-test-modal]", ".jobs-easy-apply-modal",
                                    ".artdeco-modal", "[role='dialog']"]:
                            try:
                                el = page.locator(sel).first
                                if await el.count() > 0 and await el.is_visible():
                                    modal_open = True
                                    break
                            except Exception:
                                continue
                        if modal_open:
                            result["easy_apply"] = True
                            logger.info("  ⚡ Easy Apply modal confirmed open")
                        else:
                            result["error"] = "Apply clicked but no modal and no external URL captured"
                            logger.info("  ⚠ No modal and no external URL")

        except Exception as e:
            import traceback
            err = str(e) or repr(e) or type(e).__name__
            result["error"] = err
            logger.error(f"  detect_easy_apply error: {err}\n{traceback.format_exc()}")
        finally:
            await browser.close()

    return result


# ── Bot helper — used by apply_handler.py ────────────────────────────────────

async def resolve_job_url(
    url: str,
    gemini_client=None,
    model: str = "gemini-3.5-flash",
    on_need_login: Callable = None,
) -> dict:
    """Wraps detect_easy_apply for apply_handler.py compatibility."""
    if "linkedin.com" not in url.lower():
        return {"apply_url": url, "portal": identify_portal(url),
                "easy_apply": False, "job_title": "", "company": ""}
    result = await detect_easy_apply(url)
    result["linkedin_url"] = url
    result["source"] = "easy_apply" if result["easy_apply"] else "detected"
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    if len(sys.argv) > 1 and sys.argv[1] == "login":
        load_dotenv()
        email    = os.getenv("LINKEDIN_EMAIL", "").strip()
        password = os.getenv("LINKEDIN_PASSWORD", "").strip()
        print(f"Opening LinkedIn login{' as ' + email if email else ''}… "
              "(auto-fills if creds set; solve any CAPTCHA in the window)")
        success = do_login(email, password)   # sync, saves linkedin_cookies.json
        print("✅ Session saved" if success else "❌ Login not completed")
        sys.exit(0)

    url = sys.argv[1] if len(sys.argv) > 1 else ""
    url = "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4382633406"
    if not url:
        print("Usage: python linkedin_url_extractor.py <linkedin_job_url>")
        print("       python linkedin_url_extractor.py login")
        sys.exit(1)

    result = asyncio.run(detect_easy_apply(url, debug=True))
    if result["easy_apply"]:
        print("⚡ EASY APPLY")
    else:
        print(f"🌐 EXTERNAL: {result['apply_url'] or '(not found)'}")
        print(f"   Portal: {result['portal']}")
    if result["error"]:
        print(f"❌ Error: {result['error']}")
