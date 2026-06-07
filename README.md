# CareerAI — Agentic Job Application Engine

> Paste a job URL. The agent tailors your résumé to that role, then browses the company portal, fills every field, and submits — live, in your browser.

**Theme: Agentic Web** &nbsp;|&nbsp; Works on Greenhouse · Lever · Ashby · Workday · LinkedIn Easy Apply

---

## What It Does

| Step | What happens |
|---|---|
| 1. Paste job URL | Agent scrapes the job description (LinkedIn via saved cookies) |
| 2. Résumé tailoring | Gemini scores base résumé 0–100, rewrites it with REUSE + STAR framework |
| 3. ATS check | Scores parse rate, keyword density, formatting — auto-fixes |
| 4. Auto-apply | Playwright agent fills every form field, handles dropdowns, uploads PDF, submits |
| 5. Live stream | Screenshots + agent log stream to your browser in real time over SSE |
| 6. Human-in-the-loop | Agent pauses and asks you if it hits an auth wall or ambiguous field |

No upload required for judges — a demo résumé is pre-loaded. Paste any job URL and hit **Apply →**.

---

## Architecture

```
Browser (judge)
    │  paste job URL
    ▼
FastAPI  (app.py)                              SSE stream back ──────────────┐
    ├─ scraper.py        Selenium → job description                          │
    ├─ parser.py         Gemini File API → résumé text                       │
    ├─ prompts.py        REUSE + STAR rewrite prompts                        │
    ├─ resume_pdf.py     FPDF2 → tailored PDF                                │
    ├─ ats_checker.py    ATS score (parse rate · keywords · formatting)      │
    └─ apply_handler.py  routes by URL type                                  │
           ├─ LinkedIn URL → linkedin_url_extractor → Easy Apply engine      │
           └─ External URL → auto_agent.run_autonomous_apply ────────────────┘
                                    │
                          Playwright (Chromium, headful)
                          observe → plan (1 LLM call/page) → act
                          workday.py  (deterministic Workday prefill)
                          job_wiki.py (per-portal + cross-portal memory)
                          profile_manager.py (learned answers)
```

**Key design choices:**

- **DOM-first observation** — JS pierces shadow DOM, labels every interactive element, Pillow draws numbered red boxes ("set-of-marks") on the screenshot. The LLM sees the element list; screenshot attached only when DOM is sparse or the agent is stuck — cheaper and more reliable.
- **One LLM call per page** — planner returns a full JSON plan (`page_type`, `actions[]`, `advance`) in a single call, not a tool-loop.
- **Persistent memory** — learned answers (postal code, visa status, salary) saved to `profile.json` and reused on all portals. Per-portal and cross-portal (by ATS type) lessons stored in `job_wiki.json`.
- **Human-in-the-loop over SSE** — agent runs in a background worker thread, questions routed through `queue.Queue`; answers POSTed back from the browser.

---

## AI Tools Used

| Tool | Purpose |
|---|---|
| **Google Gemini** (`gemini-3.5-flash`) | Résumé parse (File API), job match scoring, REUSE+STAR rewrite, ATS fix, profile extraction |
| **Azure OpenAI** (`gpt-5.4-mini`) | Swappable apply-agent brain (`APPLY_LLM=openai`) — plan each page, fill fields |
| **Gemini** (fallback) | Apply agent when `APPLY_LLM=gemini` |

Clean split: résumé pipeline always uses Gemini; only the browser agent is swappable via `APPLY_LLM` in `.env`.

---

## Setup

### Prerequisites
- Python 3.11+, [`uv`](https://github.com/astral-sh/uv)
- Chromium (installed by Playwright)
- DejaVu fonts in `fonts/` ([download](https://dejavu-fonts.github.io/))

### 1. Clone & install

```bash
git clone https://github.com/Mustangs007/careerai.git
cd careerai
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
APPLY_PASSWORD=...                 # filled on portals, never stored
```

### 3. Add demo résumé

Place your base résumé at `samples/demo_resume.pdf`.

### 4. Run

```bash
uv run uvicorn app:app --port 8000
# open http://localhost:8000
```

**Demo flow:** open the landing page → paste any job URL → watch the agent tailor and apply live.

### LinkedIn (optional)

```bash
uv run python linkedin_url_extractor.py login
# Sign in once — session saved to linkedin_cookies.json
```

---

## Dependencies

| Package | Version | Role |
|---|---|---|
| `fastapi` | ≥0.111 | Web backend |
| `sse-starlette` | ≥1.6 | Server-Sent Events |
| `uvicorn` | ≥0.29 | ASGI server |
| `google-genai` | ≥0.8 | Gemini API |
| `openai` | ≥1.30 | Azure OpenAI |
| `playwright` | ≥1.44 | Browser automation |
| `selenium` | ≥4.21 | Job description scraping |
| `fpdf2` | ≥2.7 | PDF generation |
| `pdfplumber` | ≥0.11 | PDF reading |
| `pymupdf` | ≥1.24 | PDF fallback |
| `pillow` | ≥10.3 | Screenshot annotation |
| `beautifulsoup4` | ≥4.12 | HTML parsing |
| `python-dotenv` | ≥1.0 | Env config |

Full pinned versions in `uv.lock`.

---

## File Map

```
app.py                  FastAPI backend + judge-facing landing page
auto_agent.py           Autonomous apply agent (observe→plan→act loop)
apply_orchestrator.py   Unified entry point for all portals
apply_handler.py        Routing: LinkedIn vs external
apply_llm.py            LLM router (Gemini ⇄ Azure OpenAI)
workday.py              Deterministic Workday prefill
linkedin_easy_apply.py  LinkedIn Easy Apply engine
job_wiki.py             Per-portal + cross-portal memory
profile_manager.py      User profile storage + learned answers
apply_skills/           Shared browser action dispatcher
parser.py               Gemini File API résumé parser
scraper.py              Selenium job-description scraper
ats_checker.py          ATS scorer
prompts.py              All Gemini prompts
resume_pdf.py           PDF generator
templates/index.html    Full résumé builder / apply UI
samples/demo_resume.pdf Base résumé for judges
deploy/                 Azure VM setup scripts
```

---

## Deploy (Azure VM)

```bash
# On VM (Ubuntu 22.04):
git clone <repo> ~/careerai && cd ~/careerai
scp .env user_profiles/ linkedin_cookies.json azureuser@<vm>:~/careerai/
bash deploy/setup.sh
# Edit /etc/caddy/Caddyfile — replace YOUR_FQDN
sudo systemctl reload caddy
```

Service runs on port 8000 behind Caddy (auto-HTTPS via Let's Encrypt).

---

## Team

| Name | Role | Contact |
|---|---|---|
| **Deepak Kumar** | Full-stack + AI/ML (solo) | [linkedin.com/in/mustangs007](https://linkedin.com/in/mustangs007) · IIT Hyderabad |
