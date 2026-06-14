# CareerAI — Agentic Job Application Engine

> Paste a job URL. The agent tailors your résumé to that role, then drives a real browser through the company's application portal — filling every field, handling custom dropdowns, uploading the PDF, and submitting — while you watch it live.

**Theme: Agentic Web** &nbsp;|&nbsp; Works on Greenhouse · Lever · Ashby · Workday · LinkedIn Easy Apply

🔗 **Live demo:** https://jarvis-apply-demo.centralindia.cloudapp.azure.com/
&nbsp;|&nbsp; **Repo:** https://github.com/mustangs0786/Jarvis (public)

---

## What It Does

| Step | What happens |
|---|---|
| 1. Paste a job URL | Scrapes the job description (LinkedIn via saved cookies) |
| 2. Résumé tailoring | Gemini scores the base résumé 0–100 and rewrites it (REUSE + STAR), then renders a tailored PDF |
| 3. ATS check | Scores parse rate, keyword coverage, formatting — and auto-fixes |
| 4. Auto-apply | A Playwright agent fills every field, drives custom widgets, uploads the résumé, and submits |
| 5. Live stream | The agent's real browser streams to your screen in real time over SSE |
| 6. Human-in-the-loop | If it gets stuck (auth wall, CAPTCHA, ambiguous field), it self-heals with vision — and if that fails, asks you |

No upload needed for judges — a demo résumé is pre-loaded. Paste any job URL and hit **Apply →**.

---

## Architecture Overview

```
Browser (user)
   │  paste job URL                         ┌──── SSE stream (log + live frames) ◄──┐
   ▼                                        │                                       │
FastAPI (app.py)                            │                                       │
   ├─ scraper.py          job description (Selenium)                                │
   ├─ parser.py           résumé text (Gemini File API)                            │
   ├─ prompts.py          REUSE + STAR rewrite prompts                             │
   ├─ resume_pdf.py       tailored PDF (FPDF2)                                      │
   ├─ ats_checker.py      ATS score + auto-fix                                      │
   └─ apply_orchestrator.run_application()  ──── unified apply engine ─────────────┘
          │
          ├─ LinkedIn URL  → linkedin_easy_apply.run_easy_apply()
          └─ Any portal    → per-page loop over apply_engine.converge_page():
                               1. deterministic fill  (apply_engine + workday.py)
                               2. page's own validation errors = the to-do list
                               3. vision audit        (apply_vision: fix wrong values)
                               4. vision recover       (stuck → screenshot-grounded fix)
                               5. grounded HTML+vision (failed field → read its HTML, fix)
                               6. human-in-the-loop    (still stuck → ask the user)
                             screencast.py streams the live browser the whole time
```

**Key design choices**

- **Deterministic-first, LLM-as-fallback.** Each page is converged by filling everything we can deterministically (stable selectors, Workday `data-automation-id`s), then treating the page's *own* validation errors as the to-do list. The LLM is the fallback resolver, not a blind page planner — cheaper and far more reliable.
- **Universal "stuck ladder."** Every stuck branch follows one rule: **try → HTML + screenshot (vision) → retry → ask the human.** A single helper (`_recover_or_ask`) is wired into the no-progress guard, redirect-loop guard, and field-level failures, so the agent never dies silently or loops forever.
- **Library-agnostic widgets.** Custom dropdowns (react-select, intl-tel-input phone-country, typeaheads) are driven by exact-match option selection — e.g. it picks **India (+91)**, not the substring trap "British **Indian** Ocean Territory (+246)."
- **Persistent memory.** Learned answers (postal code, visa status, salary) are saved to `profile.json` and reused across portals; per-portal and per-ATS lessons live in `job_wiki.json`.
- **Real-time live view.** The agent runs in a background worker thread; `screencast.py` streams JPEG frames of the *frontmost tab* over SSE (coalesced so it never lags), and human questions route through `queue.Queue`. Chromium is launched with occlusion/background-throttling disabled so the view stays in sync even when the browser window is hidden.

---

## AI Tools Used

| Tool | Model | Purpose |
|---|---|---|
| **Google Gemini** | `gemini-3.5-flash` | Résumé parse (File API), job-match scoring, REUSE+STAR rewrite, ATS auto-fix, profile extraction |
| **Azure OpenAI** | `gpt-5.4-mini` | The browser apply-agent brain (default `APPLY_LLM=openai`) — form analysis, field resolution, vision grounding |
| **Gemini** (swappable) | `gemini-3.5-flash` | Apply-agent brain when `APPLY_LLM=gemini` |

Clean split: the résumé pipeline always uses Gemini; only the browser agent is swappable via `APPLY_LLM` in `.env`.

**Dev tooling:** built with the help of Claude Code (Anthropic) as a coding assistant.

---

## Setup

### Prerequisites
- Python **3.13+**, [`uv`](https://github.com/astral-sh/uv)
- Chromium (installed by Playwright)
- DejaVu fonts in `fonts/` ([download](https://dejavu-fonts.github.io/))

### 1. Clone & install
```bash
git clone https://github.com/mustangs0786/Jarvis.git
cd Jarvis
uv sync
uv run playwright install --with-deps chromium
```

### 2. Configure `.env`
```env
GEMINI_API_KEY=...                 # résumé pipeline (required)

APPLY_LLM=openai                   # openai | gemini
APPLY_MODEL=gpt-5.4-mini
AZURE_OPENAI_ENDPOINT=https://<resource>.cognitiveservices.azure.com/openai/v1/
AZURE_OPENAI_KEY=...

APPLY_EMAIL=you@example.com        # filled on portals
APPLY_PASSWORD=...                 # filled on portals, never persisted
APPLY_HEADLESS=0                   # 0 = headed (needed for manual login/CAPTCHA), 1 = headless
```

### 3. Add a demo résumé
Place your base résumé at `samples/demo_resume.pdf`.

### 4. Run
```bash
uv run uvicorn app:app --port 8000
# open http://localhost:8000
```
Open the landing page → paste any job URL → watch the agent tailor and apply live.

### LinkedIn (optional)
```bash
uv run python linkedin_url_extractor.py login   # sign in once → saved to linkedin_cookies.json
```

---

## Dependencies

| Package | Role | | Package | Role |
|---|---|---|---|---|
| `fastapi` | Web backend | | `playwright` | Browser automation |
| `sse-starlette` | Server-Sent Events | | `selenium` | Job-description scraping |
| `uvicorn` | ASGI server | | `fpdf2` | PDF generation |
| `google-genai` | Gemini API | | `pdfplumber` / `pymupdf` | PDF reading |
| `openai` | Azure OpenAI | | `pillow` | Screenshot annotation |
| `beautifulsoup4` | HTML parsing | | `python-dotenv` | Env config |

Full pinned versions in `uv.lock`.

---

## File Map

```
app.py                  FastAPI backend + user-facing landing/apply UI
apply_orchestrator.py   Unified entry point — per-page loop + stuck ladder
apply_engine.py         Deterministic-first fill / error-correction engine
apply_vision.py         Vision audit + recover + grounded HTML repair
auto_agent.py           Shared browser primitives (observe, overlays, auth)
linkedin_easy_apply.py  LinkedIn Easy Apply engine
workday.py              Deterministic Workday prefill (stable automation-ids)
screencast.py           Live-view streamer (frontmost tab → SSE)
apply_llm.py            LLM router (Azure OpenAI ⇄ Gemini)
job_wiki.py             Per-portal + cross-portal memory
profile_manager.py      Profile storage + learned answers
apply_skills/           Shared browser action dispatcher
parser.py / scraper.py  Gemini résumé parser / Selenium JD scraper
ats_checker.py          ATS scorer        prompts.py   all Gemini prompts
resume_pdf.py           PDF generator     templates/index.html   web UI
samples/demo_resume.pdf Base résumé       deploy/      Azure VM setup
```

---

## Deploy (Azure VM)

The app drives a real Chromium, so it runs on an Azure VM (Ubuntu 22.04) under Xvfb, behind Caddy (auto-HTTPS), as a `systemd` service.

```bash
# On the VM:
git clone https://github.com/mustangs0786/Jarvis.git ~/Jarvis && cd ~/Jarvis
# copy gitignored secrets up: .env, user_profiles/, linkedin_cookies.json, browser_profile/
bash deploy/setup.sh
# point Caddy at your FQDN, then:
sudo systemctl reload caddy
```
Redeploy after a push: `cd ~/Jarvis && git pull && sudo systemctl restart resume-apply`. See `deploy/README.md` for the full walkthrough.

---

## Team

| Name | Role | Contact |
|---|---|---|
| **Deepak Kumar** | Full-stack + AI/ML engineering (solo) — agent design, résumé pipeline, deployment | [linkedin.com/in/mustangs007](https://linkedin.com/in/mustangs007) · IIT Hyderabad |
