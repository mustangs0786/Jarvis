# 360° Hackathon Readiness Report — CareerAI
*Generated 2026-06-11 after overnight E2E testing across 5 portals + LinkedIn*

## TL;DR

**Demo-ready: yes — with the right script.** LinkedIn Easy Apply submits end-to-end on autopilot (verified live, multiple times). Greenhouse fills complete applications including résumé upload and clicks Submit (one EEO-widget blocker left). HPE/Phenom fills full multi-step forms. Workday stops cleanly at its login wall (your human-in-the-loop story). SmartRecruiters is not supported yet.

## Scoreboard

| Portal | Verdict | Evidence |
|---|---|---|
| **LinkedIn Easy Apply** | ✅ **Submits end-to-end, autopilot, verified** | 3 successful submissions (Roku, Deloitte-posting, +1); screening questions answered from résumé; submit verified against LinkedIn's confirmation |
| **Greenhouse** (Jumio) | 🟡 99% — fills 21 fields incl. résumé upload, EEO dropdowns, consent; clicks Submit | Blocked by ONE field: ethnicity multi-select (details below) |
| **HPE / Phenom** | 🟡 Fills full multi-step form (14 fields, native selects, iframe upload) | Multi-step advance verified to step 4; session-state on reruns muddies repeat tests |
| **Greenhouse** (Glean) | 🟠 Form has two traps (consent-label link → glean.com; osano cookie overlay) | Both root-caused and guarded; not re-verified end-to-end |
| **Workday** (Kyndryl) | 🔴 Hard wall: account login/creation (email verification) | Stops cleanly, asks the user — correct behavior, not a bug |
| **SmartRecruiters** (Sandisk) | 🔴 Not supported: form fields not detected after gateway | Needs collector work (likely shadow-DOM/iframe variant) |

## What was fixed in this session (engine-wide)

**LinkedIn Easy Apply engine:**
1. Submit confirmation now happens BEFORE clicking (was after — couldn't cancel)
2. Real submission verification (confirmation dialog / Applied badge — no more "applied" substring false-positives)
3. Cancelled status no longer clobbered to failed; recovery-path submits counted
4. Modal detection fixed (text fallback matched every job page)
5. Profile values reach the dispatcher; `0` is a valid answer; `wait` can't crash runs
6. Apostrophe-safe selectors ("Bachelor's degree")
7. GPT (gpt-5.4-mini) is the primary model per APPLY_LLM=openai; Gemini is fallback — both ways, either provider dying never stalls a run
8. Autocomplete vision check only on real typeaheads (was: every field — 6× cost)
9. Autopilot mode (default ON): asks only when stuck; submits without confirmation

**External engine (Greenhouse/Workday/HPE/…):**
10. Native `<select>` handling: never click-to-open (OS popup froze whole runs); options read from DOM; placeholder/empty-value match bugs fixed
11. Gateway vision clicks cross-checked by label (clicked "Teams" while claiming "Apply Now")
12. Origin guard: in-form links (arbitration/privacy) can't drag the agent off the application
13. Consent checkboxes set via JS — label clicks hit embedded links
14. Chat-widget + site-search guards (agent messaged HPE's recruiter bot; typed name into search)
15. Retry caps: 4 fill-attempts/page, 2 passes/URL, progress = NEW fields only (kills runaway loops)
16. Sensitive EEO questions can never reach the LLM (it guessed "Asian" from an Indian profile) — always decline-to-answer
17. Headless + isolated-profile support for parallel testing (`APPLY_HEADLESS`, `APPLY_PROFILE_DIR`)

**App/UX:** live browser streaming (~1.4fps) with LIVE badge + theater mode + click-to-zoom; quick-reply buttons; progress tracker; friendly errors; résumé-parse disk cache (5-min stall eliminated); engine logs visible in terminal; result-card crash fixed.

## Known remaining issues (with fix plans)

1. **Greenhouse ethnicity multi-select ("mark all that apply")** — react-select multi: typed decline text snaps to wrong option; a wrong pick was once cached and re-poisons runs (purge `_resolved` keys containing "ethnic" in `user_profiles/1/profile.json` if it recurs). Fix plan: multi-select commit needs chip-verify after Enter (assert selected chip text ≈ target before caching); hold the field otherwise.
2. **HPE reruns hit saved-draft state** — the portal resumes a prior draft; engine should detect "continue your application" pages. Workaround for demos: fresh `APPLY_PROFILE_DIR`.
3. **SmartRecruiters = DataDome CAPTCHA wall** (probe confirmed: `geo.captcha-delivery.com` frame on the apply flow). Not a collector bug — the form doesn't render until a human solves it. Engine now detects captcha frames and hands off cleanly ("solve it, reply done"). Not autonomously passable, by design.
4. **Workday account creation** — auto-login now fills Workday's `data-automation-id` login fields with `.env` creds; works wherever an account exists. Account creation + email OTP still needs a human. Pre-create a Kyndryl account before any Workday demo.
5. Sync LLM calls block the event loop → live view stutters during planning (cosmetic; `asyncio.to_thread` later).
6. Glean specifically: osano cookie overlay can intercept clicks; `dismiss_overlays` needs an osano rule if Glean is a demo target (it shouldn't be).

## Recommended demo script (hackathon)

1. **Open on the dashboard** (localhost:8000, autopilot ON, theater-mode live view)
2. **Hero demo: LinkedIn Easy Apply URL** — proven, fast, fully autonomous: tailoring score → live browser → screening questions answered → auto-submit → verified "Submitted ✅". Pre-warm: restart server, run one throwaway application first.
3. **Second act: Jumio (Greenhouse)** — watch it fill 21 fields + upload résumé live; even where it stops, the whiteboard shows exactly what it did — frame "the agent knows what it doesn't know."
4. **If asked about hard portals: Workday** — show the clean stop + "please log in" question as the human-in-the-loop design, not a failure.
5. **Avoid live:** Glean, SmartRecruiters.
6. Have `output/` screenshots + this report as backup evidence if Wi-Fi/portals misbehave.

## Next approach (post-hackathon, priority order)

1. Typed form-schema parser for LinkedIn + Greenhouse (LLM answers values, never selectors) — kills the remaining widget-commit class of bugs
2. Multi-select chip-verify (unblocks Jumio-style EEO fields)
3. Draft/resume-state detection (HPE, LinkedIn drafts)
4. SmartRecruiters collector support
5. Learned-answer write-back for autonomous runs (engine gets smarter per application)
6. `asyncio.to_thread` around LLM calls (smooth live view)
