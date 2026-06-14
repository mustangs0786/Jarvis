"""
apply_orchestrator.py — the unified, continuous auto-apply engine
=================================================================
ONE production entrypoint, `run_application(...)`, that drives the MATURE engine
(apply_engine.converge_page) across EVERY page of an application until it submits.

This is the productionized version of the debug notebooks. Where the notebook does
one page per manual cell-run, this loops automatically:

    open browser → (LinkedIn? resolve → Easy Apply / direct portal)
      → per page:  dismiss overlays → submitted? → auth wall (auto-login/handoff)
                   → gateway (landing → Apply) → reveal multi-row sections
                   → converge_page (deterministic fill + error-correction)
                   → vision audit (fix values that filled but are WRONG)
                   → advance / auto-submit / ask-user-when-stuck
      → repeat until submitted.

Routing:
  • linkedin.com            → linkedin_easy_apply.run_easy_apply (kept as-is)
  • Workday + everything else → converge_page loop (site-agnostic)

Returns an ExternalApplyResult (same shape app.py / the dashboard expect).
This fully replaces the legacy auto_agent.run_autonomous_apply / plan_page engine,
which has been removed — auto_agent now only provides the shared low-level helpers
(collect_elements, execute_action, overlays, auth, screenshots) this engine builds on.
"""
import os
import logging
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

from auto_agent import (settle, dismiss_overlays, clear_blocking_overlays,
                        switch_if_new_tab, looks_like_auth, try_auto_login,
                        try_create_account, push_shot, PROFILE_DIR, MAX_ITERS,
                        diagnose_click_failure, retry_with_diagnosis)
import apply_engine
from apply_engine import converge_page, gateway_advance, reveal_rows, resolve_field
from apply_vision import vision_audit, correct_from_audit, vision_recover, execute_user_instruction
from external_apply import ExternalApplyResult, is_submitted
from workday import (WORKDAY_SUBMIT_BUTTON, is_workday_page,
                     workday_prefill, workday_fill_dropdowns)
from profile_manager import load_profile, save_profile, log_application
from job_wiki import domain_of
from apply_llm import model_label

load_dotenv()
logger = logging.getLogger(__name__)

_CANCEL_WORDS = ("cancel", "stop", "quit", "abort")
_DONE_WORDS   = ("submit", "yes", "y", "ok", "send", "proceed", "go", "done", "continue")


async def _steer(page, question, user_id, profile, gemini_client, *,
                 on_stuck, on_notify=None, on_screenshot=None,
                 cast=None, max_turns=10):
    """Multi-turn human-in-the-loop steering.

    The live screencast keeps streaming the REAL browser the whole time (no
    pause, no competing screenshot), so the user simply watches the actual page
    and either types an instruction or takes over the browser directly.

    Loop: ask user → if "continue"/cancel break → else execute instruction →
    save learning → repeat.

    Returns "cancel" | "continue" | "" (timeout/no answer)."""
    if not on_stuck:
        return ""

    for turn in range(max_turns):
        prompt = question if turn == 0 else "What next? (or say 'continue' to hand back control)"
        ans = ((await on_stuck(prompt)) or "").strip()

        if not ans:
            return ""
        if ans.lower() in _CANCEL_WORDS:
            return "cancel"
        if ans.lower() in _DONE_WORDS:
            return "continue"

        # Execute the user's instruction
        ok = await execute_user_instruction(page, ans, gemini_client,
                                            on_notify=on_notify)

        # Save what the user taught us
        if ok:
            _save_learning(profile, user_id, page.url, ans)

    return "continue"


def _save_learning(profile, user_id, url, instruction):
    """Save a user steering action as a structured learning entry.

    Field-value instructions ("select India", "pick 3-5 years") are best
    handled by the existing _resolved cache at fill time. What we save here
    are the RAW instructions with page context so we can:
    1. Show them to the LLM as hints when stuck on a similar page later.
    2. Build a log the user can review.

    Stored in profile["_user_actions"] — a list of {url_pattern, instruction, ts}.
    Kept short (last 50) so it doesn't bloat the profile."""
    from urllib.parse import urlparse
    from datetime import datetime

    actions = profile.setdefault("_user_actions", [])
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        host = ""

    actions.append({
        "host": host,
        "instruction": instruction.strip()[:200],
        "ts": datetime.now().isoformat(timespec="seconds"),
    })

    # Keep only the last 50
    if len(actions) > 50:
        profile["_user_actions"] = actions[-50:]

    try:
        save_profile(user_id, profile)
    except Exception:
        pass


async def _click_submit(page) -> bool:
    """Click the FINAL submit button (auto-submit). Tries the explicit submit
    selectors first; deliberately does NOT touch 'Save and Continue'/'Next'
    (converge_page already handles those)."""
    sels = [
        "button:has-text('Submit Application')",
        "button:has-text('Submit application')",
        "button:has-text('Submit')",
        WORKDAY_SUBMIT_BUTTON,
        "button[type='submit']",
        "input[type='submit']",
    ]
    for sel in sels:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=6000, force=True)
                return True
        except Exception:
            continue
    return False


async def _canonical_form_url(page) -> str:
    """If the page embeds an ATS application form in an iframe, return the bare
    form's own URL so we can navigate straight to it (no marketing wrapper).
    Returns '' if the page already IS the form.

    Greenhouse embeds as job-boards.greenhouse.io/embed/job_app?for=<slug>&token=<id>
    → the clean board form is job-boards.greenhouse.io/<slug>/jobs/<id>.
    Lever/Ashby/SmartRecruiters embed the bare form URL directly as the iframe src."""
    from urllib.parse import urlparse, parse_qs
    try:
        srcs = await page.evaluate(
            "[...document.querySelectorAll('iframe')].map(f=>f.src).filter(Boolean)")
    except Exception:
        return ""
    for src in srcs or []:
        s = src.lower()
        if "greenhouse.io/embed/job_app" in s:
            q = parse_qs(urlparse(src).query)
            slug = (q.get("for") or [""])[0]
            token = (q.get("token") or [""])[0]
            if slug and token:
                return f"https://job-boards.greenhouse.io/{slug}/jobs/{token}"
            return src  # fall back to the embed itself (still chrome-free)
        if any(k in s for k in ("jobs.lever.co", "jobs.ashbyhq.com",
                                "jobs.smartrecruiters.com", "myworkdayjobs.com")):
            return src
    return ""


async def run_application(
    job_url: str,
    resume_path: str,
    user_id: int,
    gemini_client=None,
    *,
    model: str = "gemini-3.5-flash",
    pro_model: str = "",
    on_notify: Callable = None,
    on_stuck: Callable = None,
    on_screenshot: Callable = None,
    auto_submit: bool = True,
    auto_answer: bool = None,
) -> ExternalApplyResult:
    from playwright.async_api import async_playwright

    profile = load_profile(user_id)
    result = ExternalApplyResult()
    result.portal = domain_of(job_url) or "external"
    # Unattended auto-answer: the engine fills its best LLM answer for free-text /
    # low-confidence fields instead of holding (so the run never blocks). Defaults
    # to auto_submit, but can be enabled independently (e.g. fill-all, confirm-submit).
    apply_engine.AUTO_ANSWER = bool(auto_submit if auto_answer is None else auto_answer)

    def _collect_filled(res):
        for n in (res.get("filled") or []):
            if n not in result.fields_filled:
                result.fields_filled.append(n)

    # ── LinkedIn: resolve to direct portal URL or route to Easy Apply ─────────
    if "linkedin.com" in job_url.lower():
        from linkedin_url_extractor import resolve_job_url

        async def _on_need_login():
            if on_notify:
                await on_notify("🔐 LinkedIn session missing/expired. Run "
                                "`python linkedin_url_extractor.py login` once. Trying without it…")

        if on_notify:
            await on_notify("🔍 LinkedIn URL — finding the direct application page…")
        resolved = await resolve_job_url(job_url, gemini_client, model, on_need_login=_on_need_login)

        if resolved.get("easy_apply"):
            from linkedin_easy_apply import run_easy_apply
            if on_notify:
                await on_notify("⚡ LinkedIn Easy Apply detected — filling with your saved session…")
            ea = await run_easy_apply(job_url=job_url, resume_path=resume_path, user_id=user_id,
                                      gemini_client=gemini_client, model=model, pro_model=pro_model,
                                      on_stuck=on_stuck, on_screenshot=on_screenshot, on_notify=on_notify,
                                      autopilot=auto_submit)
            # Enrich with the job info we already resolved — the UI shows these
            ea.job_title = resolved.get("job_title", "") or getattr(ea, "job_title", "")
            ea.company   = resolved.get("company", "")   or getattr(ea, "company", "")
            log_application(user_id, {"job_url": job_url, "job_title": getattr(ea, "job_title", ""),
                                      "company": getattr(ea, "company", ""), "portal": "linkedin_easy_apply",
                                      "status": ea.status, "fields_filled": ea.fields_filled,
                                      "fields_skipped": ea.fields_skipped,
                                      "fields_learned": ea.fields_learned, "error": ea.error})
            return ea

        if resolved.get("apply_url"):
            job_url = resolved["apply_url"]
            result.job_title = resolved.get("job_title", "")
            result.company = resolved.get("company", "")
            result.portal = resolved.get("portal", "") or result.portal
            if on_notify:
                await on_notify(f"✅ Direct apply URL: *{result.job_title}* @ *{result.company}* "
                                f"({result.portal}). Filling…")
        elif resolved.get("error"):
            result.status = "error"
            result.error = resolved["error"]
            if on_notify:
                await on_notify(f"❌ Could not resolve job URL: {resolved['error']}")
            return result

    creds = {"email": os.getenv("APPLY_EMAIL", profile.get("email", "")),
             "password": os.getenv("APPLY_PASSWORD", profile.get("password", ""))}

    async with async_playwright() as p:
        Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=bool(int(os.getenv("APPLY_HEADLESS", "0"))),
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1280, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"))
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        from screencast import start_screencast, stop_screencast
        # Pass the CONTEXT, not a page: the screencast auto-follows the frontmost
        # tab every tick, so it never desyncs when the agent moves to a new tab
        # (Workday opens the form in a new tab) or navigates a multi-step flow.
        cast = start_screencast(ctx, on_screenshot, user_id)

        def _track_page(new_page):
            """No-op kept for call-site compatibility — the screencast now follows
            the frontmost tab on its own, so nothing needs to be tracked."""
            pass

        try:
            # Retry the first navigation on transient network/DNS errors
            # (ERR_NAME_NOT_RESOLVED, ERR_CONNECTION_RESET, timeouts) — a single
            # blip used to kill the whole run before the form ever loaded.
            for _nav in range(3):
                try:
                    await page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
                    break
                except Exception as nav_ex:
                    msg = str(nav_ex).lower()
                    transient = any(k in msg for k in (
                        "err_name_not_resolved", "err_connection", "err_network",
                        "err_timed_out", "timeout", "err_internet_disconnected"))
                    if _nav < 2 and transient:
                        if on_notify:
                            await on_notify(f"🌐 Network hiccup loading the page — retrying ({_nav+1}/3)…")
                        await page.wait_for_timeout(2500)
                        continue
                    raise
            await page.wait_for_timeout(2000)

            # Marketing careers pages embed the real ATS form in an iframe,
            # wrapped in cookie banners, popups, chat widgets and a tall hero
            # (form below the fold). Go straight to the embedded form's own URL —
            # the bare ATS form has none of that chrome. This removes a whole
            # class of failures instead of patching each wrapper.
            try:
                canon = await _canonical_form_url(page)
                if canon and canon != page.url:
                    if on_notify:
                        await on_notify("🎯 Jumping to the embedded application form…")
                    await page.goto(canon, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(1500)
                    result.portal = domain_of(page.url) or result.portal
            except Exception as ex:
                logger.debug(f"canonicalize skipped: {ex}")

            try:
                result.job_title = result.job_title or (await page.title()).split("|")[0].strip()[:80]
            except Exception:
                pass
            if on_notify:
                await on_notify(f"🤖 Agent started on *{result.portal}* · driver: {model_label()}")

            auth_prompts = 0
            auto_login_tried = False
            stuck_pages = 0
            last_url = ""
            no_progress = 0
            last_filled_sig = ()
            visited_urls = {}   # url → iteration count, detects redirect loops

            from apply_engine import _site_domain
            app_dom = ""  # locked once we're past the gateway (it>2 or first converge)

            for it in range(1, MAX_ITERS + 1):
                # Origin guard: a mis-click on an in-form link (privacy policy,
                # arbitration agreement…) navigates off the application AND looks
                # like an "advance" (URL changed). Walk back to the form. Never
                # triggers on auth pages (login domains can legitimately differ).
                if app_dom and _site_domain(page.url) != app_dom \
                        and not await looks_like_auth(page):
                    if on_notify:
                        await on_notify("↩️ Wandered off the application — going back")
                    try:
                        await page.go_back(wait_until="domcontentloaded", timeout=8000)
                        await settle(page)
                    except Exception:
                        pass

                # No-progress guard: if we've sat on the SAME url for 3 iterations
                # without ever advancing, we're thrashing (single-page form, stuck
                # dropdown, etc.) — stop instead of re-filling forever.
                if page.url == last_url:
                    no_progress += 1
                else:
                    no_progress = 0
                last_url = page.url
                if no_progress >= 2:
                    if on_notify:
                        await on_notify("⏹️ No progress after 2 full passes on this page — stopping.")
                    if result.status == "pending":
                        result.status = "incomplete"
                    break

                # Redirect-loop guard: site bounces between pages (e.g. job
                # description → form → back to job description). URLs change each
                # time so the no-progress guard above doesn't catch it.
                _canon = page.url.split("?")[0].split("#")[0].rstrip("/")
                visited_urls[_canon] = visited_urls.get(_canon, 0) + 1
                if visited_urls[_canon] >= 3:
                    if on_notify:
                        await on_notify("⏹️ Redirect loop detected — this page keeps coming back.")
                    if result.status == "pending":
                        result.status = "incomplete"
                    break

                await dismiss_overlays(page)
                await clear_blocking_overlays(page)

                if await is_submitted(page):
                    result.status = "success"
                    break

                # ── BLOCKING CAPTCHA wall (DataDome/hCaptcha interstitial — the
                # form never renders until a human solves it). Must NOT trip on
                # Greenhouse's INVISIBLE reCAPTCHA badge (a 'recaptcha.net' frame
                # present on every Greenhouse form that needs no solving). So:
                # only hand off for known blocking providers AND when the page
                # has essentially no fillable form on it.
                _blocking_cap = any(
                    any(k in (f.url or "").lower() for k in
                        ("captcha-delivery.com", "datadome", "hcaptcha.com/captcha",
                         "geo.captcha"))
                    for f in page.frames)
                if _blocking_cap:
                    try:
                        n_inputs = await page.evaluate(
                            "document.querySelectorAll('input,select,textarea').length")
                    except Exception:
                        n_inputs = 0
                    if n_inputs < 3:   # real wall: no form behind it
                        await push_shot(page, user_id, it, on_screenshot, "_captcha")
                        if on_stuck:
                            ans = ((await on_stuck("This site shows a CAPTCHA — please solve it in the "
                                                   "browser window, then reply 'done'.")) or "").strip()
                            if ans.lower() in _CANCEL_WORDS:
                                result.status = "cancelled"
                                break
                            await settle(page)
                            continue
                        result.status = "failed"
                        result.error = "Blocked by CAPTCHA (anti-bot)."
                        break

                # ── auth wall ──
                # Registration forms (≥2 password fields = password + verify):
                # try create-account first.  Sign-in forms: try login first.
                # This eliminates the wasted login → fail → navigate-back cycle
                # on portals (Workday) that show the registration form directly.
                if await looks_like_auth(page):
                    if not auto_login_tried:
                        auto_login_tried = True
                        _is_reg = False
                        try:
                            _is_reg = (
                                await page.locator("[data-automation-id='verifyPassword']:visible").count() > 0
                                or await page.locator("input[type='password']:visible").count() >= 2)
                        except Exception:
                            pass
                        _attempts = ([try_create_account, try_auto_login] if _is_reg
                                     else [try_auto_login, try_create_account])
                        for _fn in _attempts:
                            if not await looks_like_auth(page):
                                break
                            try:
                                if await _fn(page, creds, on_notify):
                                    await settle(page)
                                    await page.wait_for_timeout(2000)
                            except Exception:
                                pass
                        # Silent-failure diagnostic: if we're still on the auth
                        # page after both attempts returned True, the submit
                        # click was silently swallowed (e.g. aria-hidden button,
                        # overlay div). Capture HTML + screenshot, ask LLM to
                        # diagnose, and retry with the suggested strategy.
                        if await looks_like_auth(page):
                            try:
                                if on_notify:
                                    await on_notify("🔍 Auth submit may have failed silently — diagnosing…")
                                diag = await diagnose_click_failure(page, gemini_client)
                                if diag:
                                    logger.info(f"click diagnosis: {diag.get('diagnosis','?')}")
                                    if await retry_with_diagnosis(page, diag):
                                        await settle(page)
                                        await page.wait_for_timeout(2000)
                            except Exception as ex:
                                logger.debug(f"click diagnosis skipped: {ex}")
                        if not await looks_like_auth(page):
                            if on_notify:
                                await on_notify("🔓 Authenticated — continuing with the application.")
                            continue
                    auth_prompts += 1
                    if auth_prompts > 3:
                        result.status = "failed"
                        result.error = "Login not completed."
                        break
                    ret = await _steer(page, "I need help with login — guide me or log in "
                                       "in the browser and say 'continue'.",
                                       user_id, profile, gemini_client,
                                       on_stuck=on_stuck, on_notify=on_notify,
                                       on_screenshot=on_screenshot, cast=cast)
                    if ret == "cancel":
                        result.status = "cancelled"
                        break
                    await settle(page)
                    continue

                # ── saved-draft pages: portals (HPE/Phenom, Workday) resume a
                # previous application behind a "Continue/Resume application"
                # prompt the gateway classifier doesn't know — click it
                # deterministically so reruns don't flail on the draft wall.
                try:
                    _body = ((await page.inner_text("body")) or "")[:4000].lower()
                except Exception:
                    _body = ""
                if any(t in _body for t in ("continue your application", "resume application",
                                            "continue application", "where you left off")):
                    for _t in ("Continue your application", "Resume application",
                               "Continue application", "Resume", "Continue"):
                        try:
                            _b = page.locator(f"button:has-text('{_t}'), a:has-text('{_t}'), "
                                              f"[role=button]:has-text('{_t}')").first
                            if await _b.count() > 0 and await _b.is_visible():
                                if on_notify:
                                    await on_notify("📂 Resuming a saved application draft…")
                                await _b.click(timeout=4000)
                                await settle(page)
                                break
                        except Exception:
                            continue

                # ── gateway: landing/job-desc page → click Apply, follow tab ──
                # Only early on — once we're filling a form, re-running this just
                # burns an LLM call (and must never re-click "Apply" mid-form).
                if it <= 2:
                    _before_gw = page.url
                    page = await gateway_advance(page, ctx, gemini_client, on_notify=on_notify)
                    _track_page(page)
                    if await is_submitted(page):
                        result.status = "success"
                        break
                    # Gateway may have landed on an auth page or a different
                    # form step — restart the loop so all checks (auth, captcha,
                    # submitted) re-run on the NEW page instead of falling
                    # through to converge_page on an un-classified page.
                    if page.url != _before_gw:
                        continue

                # ── reveal multi-row sections to match the profile ──
                page = await reveal_rows(page, ctx, profile, gemini_client, on_notify=on_notify) or page
                _track_page(page)

                # ── Workday: deterministic fill of the STANDARD fields by their
                # stable data-automation-id (legalName first/last, city, phone,
                # country, phone type/code) BEFORE the generic engine runs. This is
                # the reliable Workday path — no label-guessing — that the dedicated
                # workday.py provides. converge_page then handles the rest.
                try:
                    if await is_workday_page(page):
                        wf = await workday_prefill(page, profile, on_notify=on_notify)
                        wd = await workday_fill_dropdowns(page, profile, on_notify=on_notify)
                        for a in (wf or []) + (wd or []):
                            if a not in result.fields_filled:
                                result.fields_filled.append(a)
                except Exception as ex:
                    logger.debug(f"workday prefill skipped: {ex}")

                if on_notify:
                    await on_notify(f"📋 Page {it} — filling from your profile…")

                if not app_dom:  # first form page = the application's home domain
                    app_dom = _site_domain(page.url)

                res = await converge_page(page, ctx, profile, user_id=user_id,
                                          gemini_client=gemini_client, max_attempts=4, creds=creds,
                                          on_notify=on_notify, on_screenshot=on_screenshot)
                page = res.get("page", page)
                _track_page(page)
                _collect_filled(res)
                # SPA flows (HPE/Phenom iframes) advance steps WITHOUT a URL
                # change — filling NEW fields is progress. Re-filling the SAME
                # fields on the same URL is thrash, not progress (a runaway
                # re-fill loop hit page 14 on one form before this check).
                import re as _re
                filled_sig = tuple(sorted(
                    _re.sub(r"^\[\d+\]\s*", "", f) for f in (res.get("filled") or [])))
                if filled_sig and filled_sig != last_filled_sig:
                    no_progress = 0
                last_filled_sig = filled_sig

                # ── vision audit: fix values that filled but are WRONG ──
                try:
                    audit = await vision_audit(page, profile, gemini_client)
                    if not audit.get("ok"):
                        nfix = await correct_from_audit(page, profile, audit, on_notify=on_notify)
                        if nfix:
                            res = await converge_page(page, ctx, profile, user_id=user_id,
                                                      gemini_client=gemini_client, max_attempts=2, creds=creds,
                                                      on_notify=on_notify, on_screenshot=on_screenshot)
                            page = res.get("page", page)
                            _track_page(page)
                            _collect_filled(res)
                except Exception as ex:
                    logger.debug(f"vision audit skipped: {ex}")

                await push_shot(page, user_id, it, on_screenshot, f"_p{it}")
                status = res.get("status")

                if status == "advanced":
                    result.steps_completed = it
                    stuck_pages = 0
                    continue

                # ── held fields the engine couldn't confidently resolve ──
                held = res.get("held") or []
                if held and on_stuck:
                    for h in held:
                        q = (f"Need a value for '{h.get('label','this field')}'"
                             + (f" — suggestion: {h.get('suggestion')}" if h.get("suggestion") else ""))
                        ans = ((await on_stuck(q)) or "").strip()
                        if ans.lower() in _CANCEL_WORDS:
                            result.status = "cancelled"
                            return _finish(result, user_id, job_url)
                        if ans:
                            resolve_field(profile, h.get("label", ""), h.get("value", ""), ans)
                            try:
                                save_profile(user_id, profile)
                            except Exception:
                                pass
                    continue

                # ── didn't advance, no errors → review/submit page → AUTO-SUBMIT ──
                if status == "stuck_no_errors":
                    if auto_submit and await _click_submit(page):
                        if on_notify:
                            await on_notify("🚀 Submitting application…")
                        await page.wait_for_timeout(4000)
                        page = await switch_if_new_tab(ctx, page)
                        _track_page(page)
                        await settle(page)
                        if await is_submitted(page):
                            result.status = "success"
                            break
                        # Submit click fired but page didn't actually submit —
                        # diagnose via HTML + screenshot (same aria-hidden
                        # pattern that blocks auth buttons can block final
                        # submit too).
                        try:
                            diag = await diagnose_click_failure(page, gemini_client)
                            if diag and await retry_with_diagnosis(page, diag):
                                if on_notify:
                                    await on_notify(f"🔧 Submit retry: {diag.get('diagnosis','')[:60]}")
                                await settle(page)
                                await page.wait_for_timeout(3000)
                                if await is_submitted(page):
                                    result.status = "success"
                                    break
                        except Exception:
                            pass
                        continue
                    # May have landed on a gateway/job-description page mid-flow
                    # (e.g. redirect after form submission, or a multi-step portal).
                    before = page.url
                    page = await gateway_advance(page, ctx, gemini_client, on_notify=on_notify)
                    _track_page(page)
                    if page.url != before:
                        continue
                    if await vision_recover(page, profile, res.get("errors") or [],
                                            gemini_client, on_notify=on_notify):
                        continue
                    if on_stuck:
                        ret = await _steer(page, "I'm stuck on this page — what should I do?",
                                           user_id, profile, gemini_client,
                                           on_stuck=on_stuck, on_notify=on_notify,
                                           on_screenshot=on_screenshot, cast=cast)
                        if ret == "cancel":
                            result.status = "cancelled"
                            break
                        continue
                    break

                # ── stuck WITH errors / max attempts → vision recover, then ask ──
                if status in ("stuck", "max_attempts"):
                    if await vision_recover(page, profile, res.get("errors") or [],
                                            gemini_client, on_notify=on_notify):
                        continue
                    stuck_pages += 1
                    if on_stuck:
                        ret = await _steer(page, "I couldn't clear this page's errors. Any guidance?",
                                           user_id, profile, gemini_client,
                                           on_stuck=on_stuck, on_notify=on_notify,
                                           on_screenshot=on_screenshot, cast=cast)
                        if ret == "cancel":
                            result.status = "cancelled"
                            break
                        if ret:
                            continue
                    if stuck_pages >= 2:
                        result.status = "failed"
                        result.error = "Stuck on a page that would not advance."
                        break

            # final screenshot + status
            try:
                await push_shot(page, user_id, 999, on_screenshot, "_final")
            except Exception:
                pass
            if result.status == "pending":
                result.status = "success" if await is_submitted(page) else "incomplete"
            try:
                result.job_title = result.job_title or (await page.title())[:80]
            except Exception:
                pass
        except Exception as ex:
            # The page/browser can disappear mid-run (tab closed, navigation, or
            # the user closing the window). Finalize gracefully instead of crashing.
            if "closed" in str(ex).lower() or "target page" in str(ex).lower():
                logger.info(f"  page/browser closed mid-run: {ex}")
                if result.status == "pending":
                    result.status = "incomplete"
            else:
                result.status = result.status if result.status != "pending" else "error"
                result.error = result.error or str(ex)[:200]
        finally:
            await stop_screencast(cast)
            try:
                await ctx.close()
            except Exception:
                pass

    return _finish(result, user_id, job_url)


def _finish(result, user_id, job_url):
    try:
        log_application(user_id, {
            "job_url": job_url, "job_title": result.job_title, "company": result.company,
            "portal": result.portal, "status": result.status,
            "fields_filled": result.fields_filled, "fields_skipped": result.fields_skipped,
            "fields_learned": result.fields_learned, "error": result.error,
        })
    except Exception:
        pass
    return result
