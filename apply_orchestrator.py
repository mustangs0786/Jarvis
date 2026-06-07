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
                        push_shot, PROFILE_DIR, MAX_ITERS)
import apply_engine
from apply_engine import converge_page, gateway_advance, reveal_rows, resolve_field
from apply_vision import vision_audit, correct_from_audit, vision_recover
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
                                      on_stuck=on_stuck, on_screenshot=on_screenshot, on_notify=on_notify)
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
            str(PROFILE_DIR), headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1280, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"))
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
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

            for it in range(1, MAX_ITERS + 1):
                # No-progress guard: if we've sat on the SAME url for 3 iterations
                # without ever advancing, we're thrashing (single-page form, stuck
                # dropdown, etc.) — stop instead of re-filling forever.
                if page.url == last_url:
                    no_progress += 1
                else:
                    no_progress = 0
                last_url = page.url
                if no_progress >= 3:
                    if on_notify:
                        await on_notify("⏹️ No progress after 3 passes on this page — stopping.")
                    if result.status == "pending":
                        result.status = "incomplete"
                    break

                await dismiss_overlays(page)
                await clear_blocking_overlays(page)

                if await is_submitted(page):
                    result.status = "success"
                    break

                # ── auth wall: try .env auto-login once, else hand off to user ──
                if await looks_like_auth(page):
                    if not auto_login_tried:
                        auto_login_tried = True
                        try:
                            if await try_auto_login(page, creds, on_notify):
                                await settle(page); await page.wait_for_timeout(1500)
                                if not await looks_like_auth(page):
                                    if on_notify:
                                        await on_notify("🔓 Logged in with saved credentials.")
                                    continue
                        except Exception:
                            pass
                    auth_prompts += 1
                    if auth_prompts > 3:
                        result.status = "failed"
                        result.error = "Login not completed."
                        break
                    await push_shot(page, user_id, it, on_screenshot, "_login")
                    if on_stuck:
                        await on_stuck("Please log in / create your account in the browser window, "
                                       "then reply 'done'.")
                    await settle(page)
                    continue

                # ── gateway: landing/job-desc page → click Apply, follow tab ──
                # Only early on — once we're filling a form, re-running this just
                # burns an LLM call (and must never re-click "Apply" mid-form).
                if it <= 2:
                    page = await gateway_advance(page, ctx, gemini_client, on_notify=on_notify)
                    if await is_submitted(page):
                        result.status = "success"
                        break

                # ── reveal multi-row sections to match the profile ──
                page = await reveal_rows(page, ctx, profile, gemini_client, on_notify=on_notify) or page

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

                res = await converge_page(page, ctx, profile, user_id=user_id,
                                          gemini_client=gemini_client, max_attempts=6, creds=creds,
                                          on_notify=on_notify, on_screenshot=on_screenshot)
                page = res.get("page", page)
                _collect_filled(res)

                # ── vision audit: fix values that filled but are WRONG ──
                try:
                    audit = await vision_audit(page, profile, gemini_client)
                    if not audit.get("ok"):
                        nfix = await correct_from_audit(page, profile, audit, on_notify=on_notify)
                        if nfix:
                            res = await converge_page(page, ctx, profile, user_id=user_id,
                                                      gemini_client=gemini_client, max_attempts=3, creds=creds,
                                                      on_notify=on_notify, on_screenshot=on_screenshot)
                            page = res.get("page", page)
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
                        await settle(page)
                        if await is_submitted(page):
                            result.status = "success"
                            break
                        continue
                    if await vision_recover(page, profile, res.get("errors") or [],
                                            gemini_client, on_notify=on_notify):
                        continue
                    if on_stuck:
                        ans = ((await on_stuck("I'm stuck on this page — what should I do?")) or "").strip()
                        if ans.lower() in _CANCEL_WORDS:
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
                        ans = ((await on_stuck("I couldn't clear this page's errors. Any guidance?")) or "").strip()
                        if ans.lower() in _CANCEL_WORDS:
                            result.status = "cancelled"
                            break
                        if ans:
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
