# CareerAI — Technical Documentation

A local web app that **tailors your résumé to a job** and then **auto-applies** on the
company's portal using an LLM-driven browser agent.

Two halves:
1. **Résumé pipeline** — parse → match-analyze → rewrite → PDF → ATS score. (Gemini.)
2. **Auto-apply agent** — drives a real browser to fill and submit applications, learning
   from your résumé, your profile, and past runs. (Gemini *or* Azure OpenAI, your choice.)

It runs **locally** (FastAPI + a visible Chromium window on your machine), single-user.

---

## 1. Tech stack

| Layer | Tech |
|---|---|
| Backend | FastAPI, `sse-starlette` (Server-Sent Events), `uvicorn` |
| Résumé LLM | Google Gemini (`google-genai`), Gemini File API for PDF parsing |
| Apply LLM | **Swappable**: Gemini (`gemini-3.5-flash`) **or** Azure OpenAI (`gpt-5.4-mini`) |
| Browser automation | Playwright (async, Chromium, **headful**, persistent profile) |
| Job scraping | Selenium (headless Chrome) for job descriptions; LinkedIn guest API for search |
| PDF | FPDF2 (generation), pdfplumber/pymupdf (reading) |
| Storage | SQLite (`careerai.db`), JSON files (profiles, sessions, job_wiki) |

---

## 2. High-level architecture

```
 Browser (you)  ──HTTP/SSE──▶  FastAPI (app.py)
      │                            │
      │  upload / optimize         ├─ parser.py     (Gemini File API → résumé text)
      │  ATS / apply               ├─ scraper.py    (Selenium → job description)
      │                            ├─ prompts.py    (all LLM prompts)
      │                            ├─ resume_pdf.py (tailored PDF)
      │                            ├─ ats_checker.py
      │                            └─ apply_handler.run_apply(...)
      │                                   │
      │   live screenshots (SSE)          ├─ LinkedIn URL? → linkedin_url_extractor.resolve_job_url
      │   ◀───────────────────────        │       ├─ Easy Apply  → linkedin_easy_apply.run_easy_apply
      │   questions (SSE) ◀────────        │       └─ external    ┐
      │   answers (POST) ─────────▶        └─ external portal ────┴─▶ auto_agent.run_autonomous_apply
      │                                                                    │ (the main engine)
      │                                                          ┌─────────┴──────────┐
      │                                                          │  Playwright window  │ (on your screen)
      │                                                          └─────────────────────┘
                                          memory: profile_manager (profile.json) · job_wiki.json
```

The **auto-apply agent** runs in a **background worker thread** with its own event loop
(see §9), so it can drive Playwright while the SSE stream relays live screenshots/questions.

---

## 3. File map

| File | Role |
|---|---|
| `app.py` | FastAPI backend: pages, upload, analyze/optimize (SSE), ATS (SSE), **auto-apply (SSE)**, dashboard, profile, session persistence, the human-in-the-loop answer channel. |
| `auto_agent.py` | **The autonomous apply agent** — observe→plan→act loop, set-of-marks screenshots, dropdown handling, login handoff, Workday prefill, stuck/retry, learning, cross-portal lessons. |
| `apply_llm.py` | LLM router for the apply agent: Gemini ⇄ Azure OpenAI, chosen by `APPLY_LLM`. JSON mode, vision format, secret-token handling. |
| `workday.py` | Deterministic Workday handling: field map + dropdown fills by `data-automation-id`, fingerprint detection (catches vanity domains). |
| `job_wiki.py` | Per-portal memory (`job_wiki.json`) **and** cross-portal lessons by ATS type. |
| `apply_skills/base.py` | Shared browser action dispatcher: `dispatch_action`, `run_actions`, ranked `click_option`, `json_config`, learning via `learn_answer`. |
| `apply_skills/{router,account,contact,screening,resume,review}.py` | Legacy skill engine (page classifier + per-skill prompts). Used by `external_apply.py`; the active external engine is now `auto_agent`. |
| `external_apply.py` | Older skill-based external engine. `auto_agent` **reuses its primitives**: `ExternalApplyResult`, `take_screenshot`, `is_submitted`, `_check_consent_boxes`, `click_apply_button`. |
| `apply_handler.py` | `run_apply(...)` — routes LinkedIn URLs (resolve → Easy Apply or external) and non-LinkedIn URLs (→ `auto_agent`). |
| `linkedin_easy_apply.py` | LinkedIn Easy Apply engine (cookie session, in-page modal). |
| `linkedin_url_extractor.py` | Resolve a LinkedIn job URL → direct company apply URL / detect Easy Apply; manual login to save cookies. |
| `profile_manager.py` | `user_profiles/<id>/profile.json` storage; `learn_answer`, `get_field_value`, `merge_resume_into_profile`, apply log. |
| `parser.py` | Résumé parsing via Gemini File API (PDF/DOCX/images). |
| `scraper.py` | Selenium headless scraper for job-description text. |
| `ats_checker.py` | ATS scoring (parse rate, keywords, formatting). |
| `job_fetcher.py` | LinkedIn guest-API job search (synonym-aware, India-focused). |
| `prompts.py` | All Gemini prompts + `REWRITE_THRESHOLD` + résumé profile-extraction prompt. |
| `resume_pdf.py` | Tailored résumé → PDF with a validation layer. |
| `workday_recorder.py` | Dev tool: capture a Workday flow's `data-automation-id`s for building the handler. |
| `templates/index.html` | Single-page web UI (dashboard, job feed, optimizer, résumé builder/profile, apply). |

**Generated/runtime (gitignored):** `temp_resumes/` (uploads + `sessions.json`), `output/`
(PDFs + agent screenshots), `user_profiles/1/` (the one stable profile), `runs/<domain>__<ts>/`
(per-application `todo.md` debug logs + screenshots), `browser_profile/` (persistent
Playwright session — cookies, **never passwords**), `job_wiki.json`, `careerai.db`,
`linkedin_cookies.json`.

---

## 4. Configuration (`.env`)

```env
GEMINI_API_KEY=...                 # résumé pipeline + (optionally) the apply agent

APPLY_LLM=openai                   # which model drives the browser agent: openai | gemini
APPLY_MODEL=gpt-5.4-mini           # Azure deployment name (when APPLY_LLM=openai)
AZURE_OPENAI_ENDPOINT=https://<resource>.cognitiveservices.azure.com/openai/v1/
AZURE_OPENAI_KEY=...

APPLY_EMAIL=you@example.com        # used to fill email on portals
APPLY_PASSWORD=...                 # used to fill password (never logged/stored to profile)

TELEGRAM_BOT_TOKEN=...             # legacy Telegram bot (optional)
```

**Clean split:** résumé parse/analyze/rewrite **always use Gemini**; only the *apply agent*
is swappable via `APPLY_LLM`.

---

## 5. Résumé pipeline (the reliable half)

1. **Upload** (`POST /api/upload`) — saves the PDF, `parser.parse_resume_with_gemini`
   extracts text via the Gemini **File API**, a session is created, and the profile is
   **enriched** (`_enrich_apply_profile`, see §8) and auto-filled in the UI.
2. **Analyze + Optimize** (`GET /api/analyze`, SSE) — `scraper.scrape_url_content` fetches
   the job description; `build_analysis_prompt` scores the match 0–100; if ≥
   `REWRITE_THRESHOLD` it rewrites with `build_rewrite_with_context_prompt`; else it builds
   an improvement roadmap. The tailored text → `resume_pdf.generate_resume_pdf`.
3. **ATS check** (`GET /api/ats-check`, SSE) — `ats_checker.check_ats_score` on the PDF.
4. The tailored PDF + score are stored on the session and used as the résumé for auto-apply.

---

## 6. The auto-apply agent (`auto_agent.py`) — in depth

The active engine for **external company portals**. It is a single, fully LLM-driven loop:

```
per page:  dismiss overlays → (auth wall? hand to user) → observe → (Workday prefill)
           → PLAN whole page (1 LLM call) → execute each action → advance → repeat
```

### 6.1 Observation — set-of-marks
`observe()`:
- Takes a **viewport screenshot** (not full-page — full-page misaligned the marks on
  fixed modals).
- `collect_elements()` runs JS that **pierces open shadow DOM**, finds every interactive
  element (input/textarea/select/button/links/role=button/checkbox/radio/contenteditable),
  computes a **label** (aria-label → aria-labelledby → `<label for>` → wrapping label →
  placeholder → nearest preceding text → name), captures value/options/required, and tags
  each with `data-automation-id`-style index attribute **`data-agent-idx`**.
- `annotate_screenshot()` (Pillow) draws a **red numbered box** on each element (the
  "set-of-marks"), so the LLM — and you, in the live view — can reference elements by index.
- DOM-first: the **element list is the primary signal**; the screenshot is attached to the
  LLM **only when** the DOM is sparse (`n_fields == 0`) or we're stuck — cheaper + more
  reliable per industry benchmarks. If `n_fields == 0`, it waits for `networkidle` and
  re-observes (Workday-style async render) before concluding the page is empty.

### 6.2 Planning — one LLM call per page
`plan_page()` sends the element list (+ screenshot only if needed) + candidate data +
recent log to the LLM via `apply_llm.llm_json` and gets back **one JSON plan**:
```json
{ "page_type": "login|register|form|upload|review|submitted|other",
  "actions": [ {"index": N, "action": "fill|click|select|upload|ask_user", "value": "...", "label": "..."} ],
  "advance": {"action": "next|submit|none", "index": N} }
```
The system prompt encodes the **domain knowledge**: use PROFILE/RESUME/LEARNED; secret
tokens `<EMAIL>`/`<PASSWORD>`; account-first; visa→No / authorized→Yes / 18+→Yes; tick
consent; fill required fields; ask only as last resort; "submit" is only the final
submit button.

### 6.3 Execution
`execute_action()` substitutes secret tokens then calls `apply_skills.base.dispatch_action`,
targeting `[data-agent-idx="N"]` (Playwright pierces shadow DOM). Supported: fill (with
**blur** to trigger validation), click, **click_option** (custom dropdowns: open → type to
filter → **ranked match** exact>starts-with>word>substring → scroll into view → click),
upload (uses the tailored résumé PDF), scroll.

### 6.4 Advancing & the forward-button fallback
After actions, it clicks the planner's `advance` button. **Guard:** a `submit` is only
treated as the *final* submit if the button text contains "submit" on a form/review page —
otherwise it's downgraded to `next` (so "Save and Continue" / "Sign In" don't trigger a
false submit-confirmation). If the planner names no usable forward button, a deterministic
`click_forward_button()` finds the page's primary button ("Create Account / Continue / Save
and Continue / Next"), **waits up to 5s for it to enable**, and **force-clicks via JS** if a
normal click is intercepted.

### 6.5 Login / auth handoff (we never store credentials)
`looks_like_auth(page)` detects a login wall (visible password field, or URL like
`login`/`b2clogin`/`okta`/`auth`). When hit, the agent **brings the real Chrome window to
the front** and asks **you** to sign in / create the account yourself, then reply "done".
The **persistent browser profile** keeps the session, so it's a **one-time-per-portal**
step. We store the *session*, never the password. (This is why Workday/Accenture-B2C no
longer loop on login.)

### 6.6 Workday handling (`workday.py`)
- `is_workday_page(page)` detects Workday by **fingerprint** (`data-automation-id`
  presence / known ids), so it also catches Workday on **vanity domains** (e.g. Blue Yonder).
- `workday_prefill()` fills standard fields by exact id (`legalNameSection_firstName`,
  `addressSection_city`, `phone-number`, …) from the profile — deterministic, no LLM.
- `workday_fill_dropdowns()` sets Country / Phone Device Type / Country Phone Code via
  `click_option`, **guarded** to only fire when the dropdown is still "Select One".
- Anything not covered falls back to the LLM plan.

### 6.7 Stuck handling — page-level, capped at 3
**Progress = the page advanced** (navigated). Filling fields is *not* progress (otherwise
re-trying values would reset the counter forever). After **3** non-advancing tries on a
page it **asks you once** (with the marked screenshot, suggesting the likely cause —
postal code, phone format, a dropdown). If your hint still doesn't get past, it **stops**
(no churn). `MAX_ITERS = 22` bounds the whole run.

### 6.8 Submit
Only a real "Submit"/"Submit Application" on a form/review page triggers the confirmation:
*"Ready to submit to <portal>? reply submit / cancel / or tell me what to fix."* A non-
submit reply is treated as an **instruction** (fed to the planner), not a cancel.

### 6.9 The `todo.md` run log
Every run writes `runs/<domain>__<timestamp>/todo.md`: each page, each field with the
**value filled** (secrets masked), validation errors, stuck points, questions asked +
your answers, and the final status. This is the human-readable debug trail.

---

## 7. Apply routing (`apply_handler.run_apply`)

```
url is linkedin.com? ─ yes ─▶ linkedin_url_extractor.resolve_job_url
                               ├─ Easy Apply        → linkedin_easy_apply.run_easy_apply
                               ├─ resolved apply_url → (treated as external)
                               └─ otherwise          → linkedin_easy_apply (open page)
                     ─ no ──▶ external portal ───────▶ auto_agent.run_autonomous_apply
```
`auto_agent` and `linkedin_easy_apply` share the same callback contract: `on_notify`
(progress), `on_stuck` (ask the user), `on_screenshot` (live view).

---

## 8. Memory & learning (gets smarter over time)

Three distilled, always-loaded stores — **not** raw-log re-reading:

1. **Profile** — `user_profiles/1/profile.json` (one **stable** profile; `APPLY_USER_ID = 1`,
   not a per-session hash, so learning persists and there's one folder).
   - **Résumé-enriched** on upload via `_enrich_apply_profile` + `prompts.build_profile_extract_prompt`
     (name, email, phone, city, country, linkedin, github, portfolio, current title/company,
     years, **degree, university, graduation_year, cgpa, and an `education[]` list with ALL
     degrees**). Self-enriches during apply too if anything's missing.
   - **Learned answers** — `learn_answer(user_id, label, value)` saves every field the agent
     fills and every answer you give into the profile's `screening` dict; reused on **all**
     portals. (Things absent from a résumé — postal code, street address — are asked once,
     then remembered.)
   - The full profile (incl. `education[]`) + the **full résumé text** are fed into the
     agent's planning context.

2. **Per-portal memory** — `job_wiki.json` keyed by domain (subdomains like
   `jobs.`/`careers.`/`www.` collapse to one key). Stores: `account_created` (so it logs in
   instead of re-registering), `fields` filled, `qa` (questions + answers), `stuck` points
   (surfaced to the planner as `known_issues_here`), last status, and the `todo_dir`.

3. **Cross-portal lessons** — `job_wiki.json["__lessons__"]` keyed by **ATS type**
   (`portal_type()` → workday / phenom / smartrecruiters / icims / greenhouse / lever /
   sap-b2c / oracle-taleo / generic). When you resolve a stuck point with a hint, it's saved
   as a distilled tip for that *type* and **loaded for any new portal of the same type** —
   so a brand-new Workday benefits from a past one.

---

## 9. Human-in-the-loop over SSE (the tricky part)

The apply runs in a **background worker thread** with its **own `ProactorEventLoop`** (on
Windows, uvicorn's loop is a `SelectorEventLoop` that can't spawn the browser subprocess —
this caused the original `NotImplementedError`). Communication uses two thread-safe queues:

```
worker thread (agent)                         main loop (SSE generator)
   on_notify/on_screenshot ──▶ event_q ──────▶ yields SSE events to the browser
   on_stuck(question) ──▶ event_q (question) ─▶ browser shows it + the screenshot
   on_stuck waits ◀── answer_q ◀── POST /api/apply-answer ◀── you type an answer
```
`APPLY_ANSWERS[session_id]` holds the answer queue. Answer timeout is **15 min** (room to
do email verification). The UI keeps a **persistent `<img>`** for the live view and updates
only its `src` (so a new screenshot doesn't reset your scroll).

---

## 10. Web API (selected endpoints)

| Method · Path | Purpose |
|---|---|
| `GET /` | The single-page UI. |
| `POST /api/upload` | Upload résumé → parse → create session → enrich/auto-fill profile. |
| `GET /api/analyze?session_id&job_url&save` (SSE) | Scrape → score → rewrite → PDF. |
| `GET /api/ats-check?session_id` (SSE) | ATS scoring of the tailored PDF. |
| `GET /api/auto-apply?session_id&job_url` (SSE) | Run the apply agent; streams notify/shot/question/result. |
| `POST /api/apply-answer` | Deliver your answer to a stuck-question. |
| `GET /api/jobs` / `GET /api/job-meta` | LinkedIn job search + filter options. |
| `GET /api/dashboard?session_id` · `PATCH/DELETE /api/application/{id}` | Application tracker (SQLite). |
| `GET/POST /api/profile/{session_id}` | Profile read/save (SQLite). |
| `GET /api/linkedin-status` · `GET /api/linkedin-login` | LinkedIn cookie status / manual login. |

**Sessions** are in-memory but **persisted** to `temp_resumes/sessions.json` and reloaded on
startup, so `--reload` / restarts don't drop them ("Session expired").

---

## 11. Honest limitations

- **Auth walls are human-driven by design.** Login, account creation, MFA, email/CAPTCHA
  verification require *you* (the agent hands off the live window). No tool fully automates
  these — even commercial ones don't. The persistent session makes it once-per-portal.
- **Résumés lack some form data** (postal code, street address) — asked once, then learned.
- **Workday is the hardest ATS.** The deterministic handler + fingerprint detection cover the
  common path; exotic widgets (calendar pickers, multi-select skill trees) may still need a
  hint.
- **Cloud hosting would change everything** — the visible local window is what makes the
  login handoff work. A cloud deployment would need a managed browser + an embedded live-view
  (Browserbase-style) and incurs per-minute browser cost + anti-bot concerns. Local is the
  intended mode.
- **Single shared `browser_profile`** → run **one application at a time** (Chromium locks the
  profile dir).
- `gpt-5.4-mini` vs Gemini Flash is an A/B you can flip with `APPLY_LLM`; the bottleneck is
  usually auth/data, not model intelligence.

---

## 12. Running it

```bash
uv run uvicorn app:app --reload --port 8000      # or: .venv\Scripts\activate && uvicorn app:app --reload --port 8000
# open http://localhost:8000
```
Prereqs: `.env` filled (§4), `playwright install chromium`, fonts in `fonts/`.

**Demo flow:** upload résumé once in **Résumé Builder** (enriches profile) → optimize against
a job → ATS → Apply. First time on a portal you log in once in the Chrome window; after that
it's reused. Watch the live view (red numbered boxes) and answer any prompt — your answers
are remembered.
