"""
app.py — CareerAI FastAPI backend
Run: uvicorn app:app --reload --port 8000
"""

import sys, io
# Force UTF-8 stdout/stderr on Windows so emoji in logs don't crash the process
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
if hasattr(sys.stderr, "reconfigure"):
    try: sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

import os
import json
import uuid
import queue
import logging
import sqlite3
import asyncio
import subprocess

# Engine modules (linkedin_easy_apply, apply_engine, …) log progress and errors
# via logging — without a root handler those lines vanish and failures are
# silent in the terminal. uvicorn only configures its own loggers.
logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import AsyncGenerator

from fastapi import FastAPI, UploadFile, File, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
from dotenv import load_dotenv

from scraper import scrape_url_content
from parser import parse_resume_with_gemini
from resume_pdf import generate_resume_pdf
from ats_checker import check_ats_score
from job_fetcher import fetch_jobs, TIME_FILTERS, LOCATIONS, EXP_LEVELS
from prompts import (
    build_analysis_prompt,
    build_rewrite_with_context_prompt,
    build_low_score_guidance_prompt,
    build_update_rewrite_prompt,
    build_profile_extract_prompt,
    REWRITE_THRESHOLD,
)

load_dotenv()

app = FastAPI(title="CareerAI")

TEMP_DIR   = Path("temp_resumes"); TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path("output");       OUTPUT_DIR.mkdir(exist_ok=True)
Path("static").mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/output", StaticFiles(directory="output"), name="output")
templates = Jinja2Templates(directory="templates")

SESSIONS: dict[str, dict] = {}

# Single, stable profile for the apply agent on this machine. Using a constant (not
# a per-session hash) means ONE profile folder and learning that PERSISTS across
# sessions — answers like postal code are remembered and never re-asked.
APPLY_USER_ID = 1

# ── Session persistence ─────────────────────────────────────────────────────
# SESSIONS is in-memory, so a server restart (or uvicorn --reload) would drop
# every active session and break in-flight flows with "Session expired". Persist
# to disk so sessions survive reloads. Values are all JSON-serializable.
SESSIONS_FILE = TEMP_DIR / "sessions.json"

def _load_sessions():
    if SESSIONS_FILE.exists():
        try:
            SESSIONS.update(json.loads(SESSIONS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass

def _save_sessions():
    try:
        SESSIONS_FILE.write_text(json.dumps(SESSIONS, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

_load_sessions()

# ── SQLite ────────────────────────────────────────────────────────────────────
DB_PATH = Path("careerai.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS applications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL,
                job_title   TEXT DEFAULT '',
                company     TEXT DEFAULT '',
                job_url     TEXT DEFAULT '',
                portal      TEXT DEFAULT '',
                applied_on  TEXT DEFAULT '',
                status      TEXT DEFAULT 'Applied',
                match_score INTEGER DEFAULT 0,
                final_score INTEGER DEFAULT 0,
                resume_url  TEXT DEFAULT '',
                notes       TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS profiles (
                session_id       TEXT PRIMARY KEY,
                full_name        TEXT DEFAULT '',
                email            TEXT DEFAULT '',
                phone            TEXT DEFAULT '',
                linkedin         TEXT DEFAULT '',
                github           TEXT DEFAULT '',
                city             TEXT DEFAULT '',
                current_title    TEXT DEFAULT '',
                years_experience TEXT DEFAULT '',
                notice_period    TEXT DEFAULT '',
                expected_ctc     TEXT DEFAULT '',
                current_ctc      TEXT DEFAULT ''
            );
        """)

init_db()

# ── LLM ───────────────────────────────────────────────────────────────────────
def call_llm(prompt: str, model: str = "gemini-3.5-flash") -> dict:
    import time
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"),
                          http_options={"timeout": 120_000})
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3, response_mime_type="application/json"),
            )
            raw = resp.text.strip().replace("```json","").replace("```","").strip()
            return json.loads(raw)
        except Exception as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
            else:
                # Gemini exhausted (e.g. free-tier daily quota 429) — fall back
                # to Azure GPT via the apply_llm router so tailoring still works.
                logging.warning(f"[llm] Gemini failed ({e}) — falling back to Azure GPT")
                from apply_llm import _openai_json
                return _openai_json(prompt)


# ── Demo résumé (the base the agent tailors per job) ──────────────────────────
# Judges can apply by pasting only a job URL — no upload. The agent takes this base
# résumé, tailors it to the job through the same pipeline as the UI, applies with the
# tailored copy, then deletes that throwaway file. Shipped in samples/ (not gitignored)
# so it reaches the VM; falls back to the local temp_resumes copy in dev.
DEMO_RESUME = next((str(p) for p in (Path("samples/demo_resume.pdf"),
                                     Path("temp_resumes/demo_resume.pdf")) if p.exists()), "")
_DEMO_BASE_TEXT: str | None = None


def _demo_base_text() -> str:
    """Parse the bundled demo résumé once → plain text.
    Cached in memory AND on disk (demo_resume.txt next to the PDF) so a server
    restart never re-pays the Gemini File API parse — that call took 5 minutes
    on a slow day and used to run with no timeout. Falls back to local PyMuPDF
    extraction if Gemini fails, so this never blocks the apply flow."""
    global _DEMO_BASE_TEXT
    if _DEMO_BASE_TEXT is None:
        _DEMO_BASE_TEXT = ""
        if DEMO_RESUME and Path(DEMO_RESUME).exists():
            cache = Path(DEMO_RESUME).with_suffix(".txt")
            if cache.exists():
                try:
                    _DEMO_BASE_TEXT = cache.read_text(encoding="utf-8").strip()
                except Exception:
                    _DEMO_BASE_TEXT = ""
            if not _DEMO_BASE_TEXT:
                try:
                    _DEMO_BASE_TEXT = parse_resume_with_gemini(DEMO_RESUME) or ""
                except Exception as e:
                    logging.warning(f"[tailor] Gemini resume parse failed: {e}")
                if not _DEMO_BASE_TEXT or _DEMO_BASE_TEXT.startswith("Error:"):
                    try:  # local extraction — instant, good enough to tailor from
                        import fitz
                        doc = fitz.open(DEMO_RESUME)
                        _DEMO_BASE_TEXT = "\n".join(p.get_text() for p in doc).strip()
                        doc.close()
                    except Exception:
                        _DEMO_BASE_TEXT = ""
                if _DEMO_BASE_TEXT:
                    try:
                        cache.write_text(_DEMO_BASE_TEXT, encoding="utf-8")
                    except Exception:
                        pass
    return _DEMO_BASE_TEXT


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.splitlines()
        t = "\n".join(lines[1:] if lines[0].startswith("```") else lines)
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


_LOGIN_PAGE_MARKERS = ("sign in", "join linkedin", "new to linkedin",
                       "log in to continue", "enter your password", "forgot password")

_LI_JD_SELS = [
    ".jobs-description__content",
    ".jobs-description-content__text",
    "#job-details",
    ".description__text",
    ".jobs-box__html-content",
]


def _scrape_linkedin_jd(job_url: str) -> str:
    """Scrape a LinkedIn job description using saved session cookies (headless Playwright)."""
    try:
        cookies = json.loads(Path("linkedin_cookies.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not cookies:
        return ""

    async def _fetch() -> str:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
                viewport={"width": 1280, "height": 900},
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            await ctx.add_cookies(cookies)
            page = await ctx.new_page()
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2500)
                for sel in _LI_JD_SELS:
                    try:
                        el = page.locator(sel).first
                        if await el.count():
                            txt = (await el.inner_text()).strip()
                            if len(txt) > 200:
                                return txt
                    except Exception:
                        continue
                return (await page.inner_text("body")).strip()
            finally:
                await browser.close()

    try:
        return asyncio.run(_fetch())
    except Exception as e:
        print(f"[tailor] LinkedIn JD scrape failed: {e}", file=sys.stderr)
        return ""


def _analyze_for_job(base_text: str, job_url: str) -> tuple[dict, str]:
    """Scrape the JD and score the base résumé against it. Returns (analysis, jd) —
    ({}, "") when there's no usable JD."""
    try:
        if "linkedin.com" in job_url.lower():
            # LinkedIn requires auth — use saved cookies via headless Playwright.
            jd = _scrape_linkedin_jd(job_url)
        else:
            jd = scrape_url_content(job_url) or ""
        low = jd.lower()
        # Guard: too short, or looks like a login/garbage page → treat as "no JD".
        if len(jd) < 400 or sum(m in low for m in _LOGIN_PAGE_MARKERS) >= 2:
            return {}, ""
        analysis = call_llm(build_analysis_prompt(jd, base_text)) or {}
        return analysis, jd
    except Exception as e:
        print(f"[tailor] analyze failed: {e}", file=sys.stderr)
        return {}, ""


def _rewrite_to_pdf(base_text: str, jd: str, analysis: dict) -> tuple[str, dict]:
    """Rewrite the résumé for this JD and write a VIEWABLE tailored PDF into output/
    (so the UI can show it). Returns (pdf_path, info). Falls back to the base résumé
    if the JD is missing, the match is too low to meaningfully tailor, or rewrite
    fails — so the run always proceeds. `info` carries everything the UI shows."""
    info = {
        "score":       int(analysis.get("score", 0) or 0),
        "match_level": analysis.get("match_level", ""),
        "matched":     (analysis.get("matched_skills") or [])[:6],
        "keywords":    (analysis.get("ats_keywords_to_add") or [])[:8],
        "title":       _extract_title(jd) if jd else "",
        "company":     _extract_company(jd) if jd else "",
        "tailored":    False,
    }
    try:
        if not jd or info["score"] < REWRITE_THRESHOLD:
            return DEMO_RESUME, info
        rewrite = call_llm(build_rewrite_with_context_prompt(jd, base_text, analysis)) or {}
        text = _strip_fences(rewrite.get("optimized_resume_text", ""))
        if not text:
            return DEMO_RESUME, info
        name = f"_tailored_{uuid.uuid4().hex[:8]}.pdf"
        path = str(OUTPUT_DIR / name)
        generate_resume_pdf(text, path)
        info["final_score"] = int(rewrite.get("final_score_estimate", 0) or 0)
        info["changes"]     = (rewrite.get("changes_made") or [])[:6]
        info["tailored_url"] = f"/output/{name}"
        info["tailored"]    = True
        return path, info
    except Exception as e:
        print(f"[tailor] rewrite failed: {e}", file=sys.stderr)
        return DEMO_RESUME, info


# ── Pages ─────────────────────────────────────────────────────────────────────
# Single judge-facing flow at "/": paste a job URL → the agent tailors the demo
# résumé to that job, fills the form, and submits, streaming the log + live
# screenshots back over the existing /api/auto-apply SSE endpoint (session-optional).
# The full résumé-builder UI still lives at /app for internal use but is NOT linked here.
_DEMO_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CareerAI — Agentic Web</title>
<style>
  :root{--bg:#0b1020;--card:#141b30;--line:#243049;--ink:#e7ecf6;--mut:#9fb0cc;--acc:#5b8cff;--acc2:#3ad29f;--err:#ff6b6b}
  *{box-sizing:border-box}
  body{margin:0;font:16px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    background:radial-gradient(1200px 600px at 80% -10%,#1b2540,transparent),var(--bg);color:var(--ink)}
  .wrap{max-width:1320px;margin:0 auto;padding:40px 22px 60px}
  .badge{display:inline-block;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--acc2);
    border:1px solid var(--line);border-radius:999px;padding:5px 12px;background:#0e1730}
  h1{font-size:34px;margin:16px 0 6px;letter-spacing:-.02em}
  h1 .g{background:linear-gradient(90deg,var(--acc),var(--acc2));-webkit-background-clip:text;background-clip:text;color:transparent}
  .lead{color:var(--mut);font-size:16.5px;margin:0 0 24px;max-width:760px}
  .bar{display:flex;gap:10px;margin:0 0 12px}
  .bar input{flex:1;background:#0e1730;border:1px solid var(--line);border-radius:12px;color:var(--ink);
    padding:14px 16px;font-size:15px;outline:none}
  .bar input:focus{border-color:var(--acc)}
  .bar button{background:linear-gradient(90deg,var(--acc),var(--acc2));color:#04122e;font-weight:700;
    border:0;border-radius:12px;padding:0 26px;font-size:16px;cursor:pointer}
  .bar button:disabled{opacity:.55;cursor:default}
  .chips{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 22px}
  .chip{font-size:12.5px;color:var(--mut);background:#0e1730;border:1px solid var(--line);
    border-radius:999px;padding:6px 12px;cursor:pointer}
  .chip:hover{border-color:var(--acc);color:var(--ink)}
  .chip b{color:var(--acc2);font-weight:700}
  .li{display:flex;align-items:center;gap:10px;margin:0 0 18px;font-size:13px;color:var(--mut)}
  .li .lidot{width:9px;height:9px;border-radius:50%;background:#f4c789;display:inline-block}
  .li.ok .lidot{background:var(--acc2)}
  .li button{background:#0e1730;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:6px 12px;font-size:12.5px;cursor:pointer}
  .li button:hover{border-color:var(--acc)} .li.ok button{display:none}
  .live{display:grid;grid-template-columns:minmax(300px,2fr) 3fr;gap:16px;align-items:start}
  .live.theater{grid-template-columns:minmax(220px,1fr) 4fr}
  @media(max-width:900px){.live,.live.theater{grid-template-columns:1fr}}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;min-height:130px}
  .panel h4{margin:0 0 10px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--mut)}
  #log{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:600px;overflow:auto}
  #log .l{padding:3px 0;border-bottom:1px solid #1b2540;white-space:pre-wrap;word-break:break-word}
  #log .info{color:var(--ink)} #log .sys{color:var(--mut)} #log .err{color:var(--err)} #log .ok{color:var(--acc2)}
  #shotWrap{display:none;max-height:70vh;overflow-y:auto;border-radius:10px;border:1px solid var(--line)}
  #shot{width:100%;display:block}
  #liveBadge{display:none;color:#2ecc71;font-weight:600;font-size:12px;margin-left:8px;animation:pulse 1.5s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .empty{color:var(--mut);font-size:14px}
  #result{display:none;margin:16px 0 0;border-radius:14px;padding:16px 18px}
  #result.ok{background:#0f2a1f;border:1px solid #1f5a3a} #result.bad{background:#2a1014;border:1px solid #5a1f25}
  #result h3{margin:0 0 6px;font-size:20px} #result .meta{color:var(--mut);font-size:14px}
  #result ul{margin:10px 0 0;padding-left:18px;color:var(--ink);font-size:13.5px;columns:2}
  #tailor{display:none;margin:0 0 16px;background:#0f1d33;border:1px solid var(--line);border-radius:14px;padding:16px 18px}
  #tailor h3{margin:0 0 4px;font-size:18px}
  #tailor .job{color:var(--mut);font-size:14px;margin:0 0 12px}
  #tailor .scores{display:flex;align-items:center;gap:10px;margin:0 0 6px;flex-wrap:wrap}
  #tailor .sc{background:#0e1730;border:1px solid var(--line);border-radius:10px;padding:8px 12px;font-size:13px}
  #tailor .sc b{font-size:18px;color:var(--acc2)} #tailor .arrow{color:var(--mut);font-size:18px}
  #tailor .sub{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut);margin:14px 0 6px}
  #tailor ul{margin:0;padding-left:18px;font-size:13.5px}
  #tailor .kw{display:flex;flex-wrap:wrap;gap:6px}
  #tailor .kw span{background:#0e1730;border:1px solid var(--line);border-radius:999px;padding:4px 10px;font-size:12px;color:var(--acc2)}
  #tailor .links{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}
  #tailor .links a{font-size:13px;color:var(--acc);text-decoration:none;border:1px solid var(--line);border-radius:8px;padding:6px 12px}
  #tailor .links a:hover{border-color:var(--acc)}
  #ask{display:none;margin:14px 0 0;background:#1a2238;border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  #ask .q{margin:0 0 10px;font-size:14.5px}
  #ask .row{display:flex;gap:8px}
  #ask input{flex:1;background:#0e1730;border:1px solid var(--line);border-radius:10px;color:var(--ink);padding:10px 12px;outline:none}
  #ask button{background:var(--acc);color:#04122e;font-weight:700;border:0;border-radius:10px;padding:0 18px;cursor:pointer}
  .auto-row{margin:-10px 0 16px;font-size:13.5px;color:var(--mut);display:flex;align-items:center;gap:8px}
  .auto-row input[type=checkbox]{appearance:none;-webkit-appearance:none;width:20px;height:20px;
    border:2px solid var(--mut);border-radius:5px;background:#0e1730;cursor:pointer;
    position:relative;vertical-align:middle;flex:none}
  .auto-row input[type=checkbox]:checked{background:var(--acc2);border-color:var(--acc2)}
  .auto-row input[type=checkbox]:checked::after{content:'';position:absolute;left:6px;top:2px;
    width:5px;height:10px;border:solid #04122e;border-width:0 2px 2px 0;transform:rotate(45deg)}
  .auto-row label{cursor:pointer} .auto-row b{color:var(--acc2)}
  .steps{display:flex;gap:8px;margin:0 0 14px;flex-wrap:wrap}
  .st{font-size:12.5px;color:var(--mut);border:1px solid var(--line);border-radius:999px;padding:6px 12px;background:#0e1730}
  .st.on{color:var(--ink);border-color:var(--acc)}
  .st.done{color:var(--acc2);border-color:#1f5a3a}
  .qr{display:flex;gap:8px;margin:0 0 10px;flex-wrap:wrap}
  .qb{border:1px solid var(--line);background:#0e1730;color:var(--ink);border-radius:10px;padding:8px 14px;cursor:pointer;font-size:13.5px}
  .qb:hover{border-color:var(--acc)}
  .qb.ok{border-color:#1f5a3a;color:var(--acc2)} .qb.go{border-color:var(--acc);color:var(--acc)} .qb.no{border-color:#5a1f25;color:var(--err)}
  #shot{cursor:zoom-in} #shot.zoom{position:fixed;left:2vw;top:2vh;width:96vw;height:96vh;object-fit:contain;z-index:99;background:#04060f;cursor:zoom-out}
  #shotEmpty{min-height:420px;display:flex;align-items:center;justify-content:center}
  .foot{margin-top:26px;color:var(--mut);font-size:12.5px;display:flex;gap:16px;align-items:center}
  .foot a{color:var(--mut)}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#f4c789;margin-right:6px}
</style></head>
<body><div class="wrap">
  <span class="badge">Theme · Agentic Web</span>
  <h1>CareerAI — <span class="g">apply to jobs on autopilot</span></h1>
  <p class="lead">Paste a job link. The agent tailors a résumé to that role, then browses the site,
    fills every field, and submits — across Greenhouse, Lever, Ashby, Workday and LinkedIn. Watch it live below.</p>

  <div class="bar">
    <input id="url" type="text" placeholder="Paste a job URL (Greenhouse / Lever / Ashby / Workday / LinkedIn)…"
           onkeydown="if(event.key==='Enter')start()">
    <button id="go" onclick="start()">Apply →</button>
  </div>
  <div class="chips">
    <span class="chip" onclick="fill(this)" data-u="https://job-boards.greenhouse.io/rzr/jobs/4202830009"><b>Greenhouse</b> · sample</span>
    <span class="chip" onclick="fill(this)" data-u="https://jobs.lever.co/lever"><b>Lever</b> · sample</span>
    <span class="chip" onclick="fill(this)" data-u="https://expedia.wd108.myworkdayjobs.com/search"><b>Workday</b> · sample</span>
  </div>

  <div class="auto-row"><input type="checkbox" id="auto" checked>
    <label for="auto">🤖 <b>Autopilot</b> — the agent only asks if it's stuck, and submits automatically</label></div>
  <div class="auto-row"><input type="checkbox" id="tailorToggle">
    <label for="tailorToggle">✍️ <b>Tailor résumé</b> — rewrite the résumé for this role first (off = apply directly with base résumé, faster)</label></div>

  <div class="li" id="li"><span class="lidot"></span><span id="liTxt">Checking LinkedIn…</span>
    <button id="liBtn" onclick="liConnect()">Connect LinkedIn</button></div>

  <div id="tailor"></div>
  <div id="result"></div>
  <div class="steps">
    <span class="st" id="st1">1 · Tailor résumé</span>
    <span class="st" id="st2">2 · Launch browser</span>
    <span class="st" id="st3">3 · Fill application</span>
    <span class="st" id="st4">4 · Submit</span>
  </div>

  <div class="live" id="live">
    <div class="panel"><h4>Agent activity</h4><div id="log"><div class="empty">Paste a URL and hit Apply to begin.</div></div></div>
    <div class="panel"><h4>Live view<span id="liveBadge">&#9679; LIVE</span></h4><div id="shotWrap"><img id="shot" alt="agent screenshot"></div>
      <div class="empty" id="shotEmpty">The browser screen will stream here.</div>
      <div id="ask"><p class="q" id="askQ"></p>
        <div class="qr">
          <button class="qb ok" id="qbOk" onclick="quick('ok')">&#10003; Continue</button>
          <button class="qb go" id="qbGo" onclick="quick('submit')">&#128640; Submit now</button>
          <button class="qb no" id="qbNo" onclick="quick('cancel')">&#10005; Cancel</button>
        </div>
        <div class="row"><input id="ans" placeholder="Or type an answer…"
          onkeydown="if(event.key==='Enter')answer()"><button onclick="answer()">Send</button></div></div></div>
  </div>

  <div class="foot">
    <span><span class="dot"></span> Live mode — submits real applications.</span>
    <a href="/health">service health</a>
  </div>
</div>
<script>
  var es=null, sid=null, done=false;
  function $(id){return document.getElementById(id)}
  function fill(el){ $('url').value = el.getAttribute('data-u'); $('url').focus(); }
  function log(msg,cls){
    var box=$('log'); var first=box.querySelector('.empty'); if(first) box.innerHTML='';
    var d=document.createElement('div'); d.className='l '+(cls||'info'); d.textContent=msg;
    box.appendChild(d); box.scrollTop=box.scrollHeight;
  }
  function showShot(u){ if(!u) return; $('shotEmpty').style.display='none'; $('shotWrap').style.display='block';
    $('liveBadge').style.display='inline'; $('live').classList.add('theater');
    // Preload the next frame off-screen and swap only once it has decoded —
    // assigning src directly blanks the <img> mid-load and causes the flicker.
    var nu = u + (u.indexOf('?')<0?('?t='+Date.now()):''); var pre=new Image();
    pre.onload=function(){ $('shot').src=nu; }; pre.src=nu; }
  function stop(){ if(es){es.close();es=null;} $('go').disabled=false; $('go').textContent='Apply →';
    $('live').classList.remove('theater'); }
  function start(){
    var url=$('url').value.trim(); if(!url){ $('url').focus(); return; }
    stop(); done=false;
    $('go').disabled=true; $('go').textContent='Applying…';
    $('result').style.display='none'; $('ask').style.display='none'; $('tailor').style.display='none';
    $('log').innerHTML=''; $('shotWrap').style.display='none'; $('shotEmpty').style.display='block';
    sid='judge-'+Math.random().toString(36).slice(2,10);
    log('Starting agent on '+url,'sys');
    setStage(1);
    es=new EventSource('/api/auto-apply?session_id='+encodeURIComponent(sid)+'&job_url='+encodeURIComponent(url)
       +'&autopilot='+($('auto').checked?1:0)+'&tailor='+($('tailorToggle').checked?1:0));
    es.onmessage=function(e){ var d; try{d=JSON.parse(e.data)}catch(_){return} handle(d); };
    es.onerror=function(){ if(!done) log('Stream closed.','sys'); stop(); };
  }
  var questionActive=false;
  function handle(d){
    var s=d.step;
    if(s==='shot'){ if(!questionActive) showShot(d.url); return; }
    if(s==='tailor'){ tailorCard(d); return; }
    if(s==='question'){ questionActive=true; ask(d.msg, d.shot); return; }
    if(s==='applied'){ done=true; if(d.status==='success') setStage(5); result(d); stop(); return; }
    if(s==='error'||s==='no_profile'||s==='apply_unavailable'){ done=true; log('⚠ '+(d.msg||'Error'),'err'); stop(); return; }
    if(d.msg){
      log(d.msg, s==='applying'?'info':'sys');
      if(/Launching browser/i.test(d.msg)) setStage(2);
      if(/modal opened|Filling your details|Step \d+ filled/i.test(d.msg)) setStage(3);
      if(/Review step/i.test(d.msg)) setStage(4);
    }
  }
  var okAns='ok', noAns='cancel';
  function ask(q,shot){
    q=String(q||'The agent needs input:').replace(/\*/g,'');
    $('ask').style.display='block'; $('askQ').textContent=q;
    var ql=q.toLowerCase();
    var showOk=true, showGo=false, showNo=true;
    okAns='ok'; noAns='cancel';
    if(ql.indexOf('ready to submit')>-1||ql.indexOf('reply submit')>-1){      // final confirm
      showOk=false; showGo=true;
    } else if(ql.indexOf('retry')>-1){                                        // stuck on navigation
      okAns='retry'; noAns='skip';
      $('qbOk').innerHTML='&#8635; Retry'; $('qbNo').innerHTML='&#8618; Stop here';
    } else if(ql.indexOf('step')>-1&&ql.indexOf('fix')>-1){                   // step review (supervised)
      showNo=false; $('qbOk').innerHTML='&#10003; Continue';
    } else {                                                                  // unknown field — type or skip
      showOk=false; noAns='skip'; $('qbNo').innerHTML='&#8618; Skip this field';
    }
    $('qbOk').style.display=showOk?'':'none';
    $('qbGo').style.display=showGo?'':'none';
    $('qbNo').style.display=showNo?'':'none';
    showShot(shot); $('ans').value=''; $('ans').focus(); }
  function quick(k){ sendAnswer(k==='ok'?okAns:(k==='cancel'?noAns:k)); }
  function answer(){ var a=$('ans').value.trim(); if(!a) return; sendAnswer(a); }
  function sendAnswer(a){
    fetch('/api/apply-answer',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,answer:a})});
    log('You: '+a,'ok'); $('ask').style.display='none'; questionActive=false;
  }
  function result(d){
    var ok = d.status==='success';
    var r=$('result'); r.className=ok?'ok':'bad'; r.style.display='block';
    var ff=(d.fields_filled||[]);
    var head = ok?'✅ Application submitted':'⚠️ Run finished — '+(d.status||'incomplete');
    var meta=[d.job_title,d.company,d.portal].filter(Boolean).join(' · ');
    var html='<h3>'+esc(head)+'</h3>'+(meta?'<div class="meta">'+esc(meta)+'</div>':'');
    if(d.error) html+='<div class="meta">'+esc(d.error)+'</div>';
    if(ff.length){ html+='<ul>'+ff.slice(0,30).map(function(x){return '<li>'+esc(String(x))+'</li>'}).join('')+'</ul>'; }
    r.innerHTML=html;
  }
  function tailorCard(d){
    var t=$('tailor'); t.style.display='block';
    if(d.no_jd){
      t.innerHTML='<h3>📄 Applying with base résumé</h3>'+
        '<p class="job">The job description isn\\'t readable here (the site blocks automated readers, e.g. login or anti-bot), '+
        'so the agent applies with your base résumé.</p>'+
        (d.original_url?'<div class="links"><a href="'+d.original_url+'" target="_blank">📄 Original résumé</a></div>':'');
      return;
    }
    var jl=[d.title,d.company].filter(Boolean).join(' @ ');
    var h = d.tailored ? '<h3>✨ Résumé tailored for this role</h3>' : '<h3>📄 Applying with base résumé</h3>';
    if(jl) h+='<p class="job">'+esc(jl)+'</p>';
    h+='<div class="scores"><span class="sc">Base match <b>'+(d.score||0)+'</b>/100'+(d.match_level?(' · '+esc(d.match_level)):'')+'</span>';
    if(d.tailored && d.final_score) h+='<span class="arrow">→</span><span class="sc">Tailored est. <b>'+d.final_score+'</b>/100</span>';
    h+='</div>';
    if(d.tailored && d.changes && d.changes.length)
      h+='<div class="sub">What the agent changed</div><ul>'+d.changes.map(function(c){return '<li>'+esc(String(c))+'</li>'}).join('')+'</ul>';
    if(d.keywords && d.keywords.length)
      h+='<div class="sub">JD keywords aligned</div><div class="kw">'+d.keywords.map(function(k){return '<span>'+esc(String(k))+'</span>'}).join('')+'</div>';
    var links='';
    if(d.original_url) links+='<a href="'+d.original_url+'" target="_blank">📄 Original résumé</a>';
    if(d.tailored && d.tailored_url) links+='<a href="'+d.tailored_url+'" target="_blank">✨ Tailored résumé</a>';
    if(links) h+='<div class="links">'+links+'</div>';
    t.innerHTML=h;
  }
  function esc(s){ return String(s).replace(/[&<>\"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]}); }
  function liStatus(){
    fetch('/api/linkedin-status').then(function(r){return r.json()}).then(function(d){
      var el=$('li');
      if(d && d.logged_in){ el.className='li ok'; $('liTxt').textContent='LinkedIn connected ✓'; }
      else { el.className='li'; $('liTxt').textContent='LinkedIn not connected — needed for LinkedIn URLs'; }
    }).catch(function(){});
  }
  function liConnect(){
    $('liBtn').disabled=true; $('liBtn').textContent='Opening…';
    log('Opening LinkedIn login — sign in / solve any CAPTCHA in the window that opens.','sys');
    var ls=new EventSource('/api/linkedin-login');
    ls.onmessage=function(e){ var d; try{d=JSON.parse(e.data)}catch(_){return}
      if(d.msg) log(d.msg, d.step==='error'?'err':(d.step==='done'?'ok':'sys'));
      if(d.step==='done'||d.step==='error'||d.step==='info'){ ls.close(); $('liBtn').disabled=false; $('liBtn').textContent='Connect LinkedIn'; liStatus(); }
    };
    ls.onerror=function(){ ls.close(); $('liBtn').disabled=false; $('liBtn').textContent='Connect LinkedIn'; liStatus(); };
  }
  function setStage(n){
    for(var i=1;i<=4;i++){ var el=$('st'+i); if(!el) continue;
      el.className='st'+(i<n?' done':(i===n?' on':'')); }
  }
  $('shot').onclick=function(){ this.classList.toggle('zoom'); };
  liStatus();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
async def landing():
    """The single judge-facing flow: paste a job URL → watch the agent apply live."""
    return HTMLResponse(_DEMO_HTML)


@app.get("/app", response_class=HTMLResponse)
async def index(request: Request):
    """The actual CareerAI app UI (résumé refactor + autonomous apply)."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/health")
async def health():
    """Liveness probe for Azure / Caddy / uptime checks."""
    return {"status": "ok", "service": "careerai", "time": datetime.utcnow().isoformat() + "Z"}


@app.get("/demo-resume")
async def demo_resume():
    """Serve the base demo résumé so the judge UI can show 'Original résumé'."""
    if DEMO_RESUME and Path(DEMO_RESUME).exists():
        return FileResponse(DEMO_RESUME, media_type="application/pdf", filename="demo_resume.pdf")
    raise HTTPException(404, "Demo résumé not found.")


# ── Upload ────────────────────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload_resume(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported.")
    session_id = str(uuid.uuid4())[:8]
    dest = TEMP_DIR / f"{session_id}_{file.filename}"
    dest.write_bytes(await file.read())
    resume_text = parse_resume_with_gemini(str(dest))
    if not resume_text or resume_text.lower().startswith("error"):
        dest.unlink(missing_ok=True)
        raise HTTPException(422, "Could not parse resume.")
    name = next((l.strip().lstrip("#").strip() for l in resume_text.splitlines() if l.strip()), "")
    SESSIONS[session_id] = {"resume_path": str(dest), "resume_text": resume_text,
                             "filename": file.filename, "name": name}
    _save_sessions()

    # Extract profile fields from resume and auto-save
    extracted_profile: dict = {}
    try:
        extracted_profile = call_llm(build_profile_extract_prompt(resume_text))
        if extracted_profile:
            _save_profile(session_id, extracted_profile)            # SQLite (UI fields)
            _enrich_apply_profile(extracted_profile, resume_text)   # agent profile (all fields)
    except Exception:
        pass

    return {
        "session_id": session_id,
        "name": name,
        "filename": file.filename,
        "profile": extracted_profile,
    }


# ── Analyze + Optimize (SSE) ──────────────────────────────────────────────────
@app.get("/api/analyze")
async def analyze_stream(session_id: str, job_url: str, save: bool = False):

    async def generate() -> AsyncGenerator[dict, None]:
        session = SESSIONS.get(session_id)
        if not session:
            yield {"data": json.dumps({"step": "error", "msg": "Session expired — re-upload your resume."})}
            return

        resume_text = session["resume_text"]

        # Step 1 — scrape
        yield {"data": json.dumps({"step": "scraping", "msg": "Fetching job description..."})}
        await asyncio.sleep(0.05)
        try:
            job_description = await asyncio.to_thread(scrape_url_content, job_url)
        except Exception as e:
            yield {"data": json.dumps({"step": "error", "msg": f"Scrape failed: {e}"})}
            return
        if not job_description:
            yield {"data": json.dumps({"step": "error", "msg": "Could not extract job description."})}
            return
        yield {"data": json.dumps({"step": "scraped", "msg": "Job description fetched ✓"})}

        # Step 2 — analyze
        yield {"data": json.dumps({"step": "analyzing", "msg": "Analyzing resume match..."})}
        await asyncio.sleep(0.05)
        try:
            analysis = await asyncio.to_thread(call_llm, build_analysis_prompt(job_description, resume_text))
        except Exception as e:
            yield {"data": json.dumps({"step": "error", "msg": f"Analysis failed: {e}"})}
            return
        score     = analysis.get("score", 0)
        match_lvl = analysis.get("match_level", "Unknown")
        yield {"data": json.dumps({"step": "analyzed", "msg": f"Match score: {score}/100 ({match_lvl}) ✓", "score": score})}

        # Low match path — build roadmap
        if score < REWRITE_THRESHOLD:
            yield {"data": json.dumps({"step": "roadmap", "msg": "Building improvement roadmap..."})}
            try:
                guidance = await asyncio.to_thread(
                    call_llm, build_low_score_guidance_prompt(job_description, resume_text, analysis))
            except Exception:
                guidance = {}
            yield {"data": json.dumps({
                "step": "low_match", "score": score, "analysis": analysis, "guidance": guidance,
                "msg": f"Score {score}/100 — roadmap ready.",
            })}
            return

        # Step 3 — rewrite
        yield {"data": json.dumps({"step": "rewriting", "msg": "Tailoring with FAANG-grade prompts..."})}
        await asyncio.sleep(0.05)
        try:
            rewrite = await asyncio.to_thread(
                call_llm, build_rewrite_with_context_prompt(job_description, resume_text, analysis))
        except Exception as e:
            yield {"data": json.dumps({"step": "error", "msg": f"Rewrite failed: {e}"})}
            return
        optimized_text = rewrite.get("optimized_resume_text", "")
        # Strip markdown code fences the LLM sometimes wraps around the value
        _ot = optimized_text.strip()
        if _ot.startswith("```"):
            _lines = _ot.splitlines()
            _ot = "\n".join(_lines[1:] if _lines[0].startswith("```") else _lines)
            if _ot.rstrip().endswith("```"):
                _ot = _ot.rstrip()[:-3].rstrip()
        optimized_text = _ot.strip()
        if not optimized_text:
            yield {"data": json.dumps({"step": "error", "msg": "LLM returned empty resume text — try again."})}
            return
        yield {"data": json.dumps({"step": "rewritten", "msg": "Resume tailored ✓"})}

        # Step 4 — PDF
        yield {"data": json.dumps({"step": "pdf", "msg": "Generating PDF..."})}
        await asyncio.sleep(0.05)
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        pdf_name = f"{session_id}_{ts}_resume.pdf"
        pdf_path = str(OUTPUT_DIR / pdf_name)
        pdf_url  = ""
        pdf_err  = ""
        try:
            await asyncio.to_thread(generate_resume_pdf, optimized_text, pdf_path)
            pdf_url = f"/output/{pdf_name}"
        except Exception as _pdf_e:
            pdf_err = str(_pdf_e)
            print(f"[PDF ERROR] {pdf_err}", file=sys.stderr)
        yield {"data": json.dumps({"step": "pdf_done", "msg": "PDF generated ✓" if pdf_url else f"PDF failed: {pdf_err[:120]}"})}


        SESSIONS[session_id]["result"] = {
            "analysis": analysis, "rewrite": rewrite,
            "job_url": job_url, "job_description": job_description,
            "optimized_text": optimized_text, "original_text": resume_text,
            "pdf_url": pdf_url, "pdf_path": pdf_path, "score": score,
        }
        _save_sessions()

        if save:
            _log_application(session_id, {
                "job_title":   _extract_title(job_description),
                "company":     _extract_company(job_description),
                "job_url":     job_url,
                "match_score": score,
                "final_score": rewrite.get("final_score_estimate", 0),
                "resume_url":  pdf_url,
                "status":      "Tailored",
            })

        yield {"data": json.dumps({
            "step": "done", "msg": "All done ✓",
            "analysis":       analysis,
            "rewrite":        rewrite,
            "optimized_text": optimized_text,
            "original_text":  resume_text,
            "pdf_url":        pdf_url,
            "score":          score,
            "cover_letter":   rewrite.get("cover_letter_hook", ""),
        })}

    return EventSourceResponse(generate())


# ── ATS Check (SSE) ───────────────────────────────────────────────────────────
@app.get("/api/ats-check")
async def ats_check_stream(session_id: str):

    async def generate() -> AsyncGenerator[dict, None]:
        session = SESSIONS.get(session_id)
        if not session:
            yield {"data": json.dumps({"step": "error", "msg": "Session expired."})}
            return
        result_data = session.get("result", {})
        pdf_path    = result_data.get("pdf_path", "")
        job_desc    = result_data.get("job_description", "")

        if not pdf_path or not Path(pdf_path).exists():
            yield {"data": json.dumps({"step": "error", "msg": "No PDF found — optimize first."})}
            return

        yield {"data": json.dumps({"step": "checking", "msg": "Running ATS analysis..."})}
        await asyncio.sleep(0.1)

        try:
            ats = await asyncio.to_thread(check_ats_score, pdf_path, job_desc)
            yield {"data": json.dumps({
                "step":             "ats_done",
                "overall_score":    ats.overall_score,
                "parse_rate":       ats.parse_rate,
                "grade":            ats.grade,
                "summary":          ats.summary,
                "source":           ats.source,
                "sections_found":   ats.sections_found,
                "sections_missing": ats.sections_missing,
                "keyword_hits":     ats.keyword_hits,
                "keyword_misses":   ats.keyword_misses,
                "issues":           ats.issues,
                "suggestions":      ats.suggestions,
                "strengths":        ats.strengths,
            })}
        except Exception as e:
            yield {"data": json.dumps({"step": "error", "msg": f"ATS check failed: {e}"})}

    return EventSourceResponse(generate())


# ── Resume Update (SSE) ───────────────────────────────────────────────────────
@app.get("/api/resume-update")
async def resume_update_stream(session_id: str, description: str):
    """Update resume without a job URL — new job, cert, promotion."""

    async def generate() -> AsyncGenerator[dict, None]:
        session = SESSIONS.get(session_id)
        if not session:
            yield {"data": json.dumps({"step": "error", "msg": "Session expired."})}
            return

        resume_text = session["resume_text"]
        merged      = f"{resume_text}\n\n## UPDATE TO ADD:\n{description}"

        yield {"data": json.dumps({"step": "merging", "msg": "Merging update into resume..."})}
        await asyncio.sleep(0.1)

        try:
            rewrite = await asyncio.to_thread(call_llm, build_update_rewrite_prompt(merged))
        except Exception as e:
            yield {"data": json.dumps({"step": "error", "msg": f"Update failed: {e}"})}
            return

        updated_text = rewrite.get("optimized_resume_text", "")
        yield {"data": json.dumps({"step": "rewritten", "msg": "Resume updated ✓"})}

        yield {"data": json.dumps({"step": "pdf", "msg": "Generating PDF..."})}
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        pdf_name = f"{session_id}_{ts}_updated.pdf"
        pdf_path = str(OUTPUT_DIR / pdf_name)
        pdf_url  = ""
        try:
            await asyncio.to_thread(generate_resume_pdf, updated_text, pdf_path)
            pdf_url = f"/output/{pdf_name}"
            # Update session's resume for future optimizations
            session["resume_text"] = updated_text
            session["result"] = {**session.get("result", {}),
                                  "optimized_text": updated_text, "pdf_path": pdf_path, "pdf_url": pdf_url}
            _save_sessions()
        except Exception:
            pass

        yield {"data": json.dumps({
            "step": "done", "updated_text": updated_text,
            "pdf_url": pdf_url, "changes": rewrite.get("changes_made", []),
        })}

    return EventSourceResponse(generate())


# ── Auto-Apply (SSE) ──────────────────────────────────────────────────────────
# Per-session answer channel. The apply runs in a worker thread and asks
# questions over SSE; the browser POSTs answers to /api/apply-answer, which drops
# them into the matching queue here for the worker's on_stuck to pick up.
APPLY_ANSWERS: dict[str, queue.Queue] = {}

def _friendly_error(msg: str) -> str:
    """Map raw engine/SDK errors to judge-friendly one-liners."""
    m = (msg or "").lower()
    if "429" in m or "resource_exhausted" in m or "quota" in m or "rate limit" in m:
        return "⚠️ LLM quota hit — the agent switched to the backup model. Just retry."
    if "timeout" in m and ("goto" in m or "navigation" in m or "net::" in m):
        return "⚠️ The job page took too long to load — check the URL and retry."
    if "no linkedin session" in m:
        return "🔐 LinkedIn not connected — click Connect LinkedIn above."
    if "checkpoint" in m or "li_at" in m or ("linkedin" in m and ("session" in m or "expired" in m)):
        return "🔐 LinkedIn session expired — click Connect LinkedIn and sign in once."
    return msg


def _normalize_url(u: str) -> str:
    """Make a pasted job URL navigable. Adds a scheme if missing (people paste
    'greenhouse.io/...' or 'www.company.com/jobs/1'), and fixes the common
    'ww.' typo for 'www.'. Both the Selenium scraper and Playwright reject a
    schemeless URL with 'invalid argument' / 'invalid URL'."""
    u = (u or "").strip().strip('"').strip("'")
    if not u:
        return u
    if u.startswith(("http://", "https://")):
        return u
    if u.startswith("ww.") or u.startswith("ww2."):
        u = "www." + u.split(".", 1)[1]
    return "https://" + u.lstrip("/")


@app.get("/api/auto-apply")
async def auto_apply_stream(job_url: str, session_id: str = "", autopilot: int = 1, tailor: int = 0):
    job_url = _normalize_url(job_url)

    async def generate() -> AsyncGenerator[dict, None]:
        # A session is OPTIONAL — judges can apply by pasting only a job URL. The agent
        # uses the demo résumé as its base and tailors it to this job. The tailored PDF
        # is written into output/ so the UI can show it; we keep only the LATEST one
        # (purge prior tailored files here) so nothing accumulates.
        for _old in OUTPUT_DIR.glob("_tailored_*.pdf"):
            try: _old.unlink()
            except Exception: pass
        session      = SESSIONS.get(session_id) or {}
        result_data  = session.get("result", {})
        score        = result_data.get("score", 0)
        tailored_tmp = ""   # path of the tailored PDF for this run (kept, viewable)

        # ── Profile (cheap gate first): the agent fills from the persistent demo
        # profile (user_profiles/<id>). Merge a judge's uploaded-session profile if any. ──
        from profile_manager import load_profile as _load_apply_profile
        user_id = APPLY_USER_ID   # one stable profile → persistent learning, one folder
        sess_profile = _get_profile(session_id) if session_id else {}
        if sess_profile.get("email"):
            _sync_profile_to_manager(user_id, sess_profile, resume_text=session.get("resume_text", ""))
        profile = _load_apply_profile(user_id) or {}
        if not profile.get("email"):
            yield {"data": json.dumps({"step": "no_profile",
                "msg": f"Demo profile incomplete — set email/phone in user_profiles/{user_id}/profile.json."})}
            return

        # ── Résumé: reuse a UI-tailored PDF if the judge optimized one; otherwise take
        # the demo base and tailor it to THIS job, streaming the analysis + changes so
        # the (otherwise invisible) tailoring step is visible in the demo. ──
        ui_pdf = result_data.get("pdf_path", "")
        if not tailor:
            # DEFAULT (toggle OFF): apply directly with the BASE résumé — no
            # Gemini tailoring. Faster, and what most runs want.
            if not DEMO_RESUME or not Path(DEMO_RESUME).exists():
                yield {"data": json.dumps({"step": "error",
                    "msg": "Demo résumé missing — add samples/demo_resume.pdf."})}
                return
            resume_path = DEMO_RESUME
            yield {"data": json.dumps({"step": "tailor", "tailored": False,
                "no_jd": False, "original_url": "/demo-resume"})}
            yield {"data": json.dumps({"step": "applying",
                "msg": "📄 Tailoring off — applying directly with your base résumé."})}
        elif ui_pdf and Path(ui_pdf).exists():
            # toggle ON and a UI-tailored PDF already exists → use it (tailored only)
            resume_path = ui_pdf
        else:
            # toggle ON, no prebuilt PDF → tailor the base résumé to THIS job now
            if not DEMO_RESUME or not Path(DEMO_RESUME).exists():
                yield {"data": json.dumps({"step": "error",
                    "msg": "Demo résumé missing — add samples/demo_resume.pdf."})}
                return
            yield {"data": json.dumps({"step": "applying", "msg": "📄 Reading your base résumé…"})}
            # to_thread: the Gemini File API parse can take minutes — running it
            # inline froze the event loop (no SSE pings, dead UI) for that long.
            base_text = session.get("resume_text") or await asyncio.to_thread(_demo_base_text)

            yield {"data": json.dumps({"step": "applying", "msg": "📄 Reading the job description…"})}
            analysis, jd = await asyncio.to_thread(_analyze_for_job, base_text, job_url)

            if not jd:
                # No readable JD (e.g. LinkedIn behind login) → skip tailoring, use base.
                resume_path = DEMO_RESUME
                yield {"data": json.dumps({"step": "tailor", "tailored": False,
                    "no_jd": True, "original_url": "/demo-resume"})}
                yield {"data": json.dumps({"step": "applying",
                    "msg": "ℹ️ Job description isn't readable here (site blocks readers — login or anti-bot) — "
                           "applying with your base résumé."})}
            else:
                score = int(analysis.get("score", 0) or 0)
                yield {"data": json.dumps({"step": "applying",
                    "msg": f"🎯 Base résumé match: {score}/100 ({analysis.get('match_level','—')})"})}
                yield {"data": json.dumps({"step": "applying", "msg": "✍️ Tailoring your résumé to this role…"})}
                resume_path, tinfo = await asyncio.to_thread(_rewrite_to_pdf, base_text, jd, analysis)
                tinfo["original_url"] = "/demo-resume"
                yield {"data": json.dumps({"step": "tailor", **tinfo})}
                if tinfo.get("tailored"):
                    tailored_tmp = resume_path
                    yield {"data": json.dumps({"step": "applying",
                        "msg": f"✨ Tailored — estimated match now {tinfo.get('final_score', score)}/100"})}
                else:
                    yield {"data": json.dumps({"step": "applying",
                        "msg": "ℹ️ Applying with the base résumé (couldn't meaningfully tailor)."})}

        yield {"data": json.dumps({"step": "applying", "msg": "🚀 Launching browser agent…"})}
        yield {"data": json.dumps({"step": "applying",
            "msg": ("🤖 Autopilot ON — the agent only asks if it gets stuck."
                    if autopilot else "👀 Supervised mode — you'll confirm each step and the final submit.")})}

        from google import genai
        # 120s HTTP timeout: a stalled Gemini call must fail (and surface in the
        # UI) instead of freezing the apply run forever.
        gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"),
                                     http_options={"timeout": 120_000})

        # Thread-safe channels between this SSE loop (main) and the apply worker.
        event_q:  queue.Queue = queue.Queue()   # worker → browser (notify/question/result)
        answer_q: queue.Queue = queue.Queue()   # browser → worker (answers to questions)
        APPLY_ANSWERS[session_id] = answer_q

        latest_shot = {"url": ""}  # most recent screenshot, shown with questions

        async def on_notify(msg):
            event_q.put({"type": "notify", "msg": msg})

        async def on_screenshot(path):
            # Screenshots are written under output/ which is mounted at /output.
            name = str(path).replace("\\", "/").split("/")[-1]
            latest_shot["url"] = f"/output/{name}"
            event_q.put({"type": "shot", "url": latest_shot["url"]})

        async def on_stuck(question):
            # Surface the question + current screenshot, then block until answered.
            event_q.put({"type": "question", "msg": question, "shot": latest_shot["url"]})
            loop = asyncio.get_event_loop()
            try:  # wait up to 15 min (room to check email for verification, etc.)
                return await loop.run_in_executor(None, lambda: answer_q.get(timeout=900))
            except Exception:
                # Timed out / no answer → return EMPTY (not "skip"). The agent treats this
                # as "no guidance, try once more", never as a command to stop the run.
                return ""

        def _apply_in_proactor():
            # Playwright needs a subprocess-capable loop. On Windows, uvicorn's
            # loop is a SelectorEventLoop, which raises NotImplementedError when
            # launching the browser. Run the apply on a ProactorEventLoop here.
            # run_application = the unified, continuous engine (converge_page across
            # ALL pages + vision audit + auto-submit; LinkedIn → Easy Apply).
            from apply_orchestrator import run_application
            loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                res = loop.run_until_complete(run_application(
                    job_url=job_url, resume_path=resume_path, user_id=user_id,
                    gemini_client=gemini_client, model="gemini-3.5-flash",
                    pro_model="gemini-3.5-flash", auto_submit=bool(autopilot),
                    on_notify=on_notify, on_stuck=on_stuck, on_screenshot=on_screenshot,
                ))
                event_q.put({"type": "result", "result": res})
            except Exception as e:
                import traceback
                msg = str(e) or repr(e) or traceback.format_exc().strip().splitlines()[-1]
                event_q.put({"type": "error", "msg": msg})
            finally:
                loop.close()

        worker      = asyncio.create_task(asyncio.to_thread(_apply_in_proactor))
        main_loop   = asyncio.get_event_loop()
        notifications: list = []

        try:
            while True:
                item = await main_loop.run_in_executor(None, event_q.get)
                kind = item["type"]

                if kind == "notify":
                    notifications.append(item["msg"])
                    yield {"data": json.dumps({"step": "applying", "msg": item["msg"]})}

                elif kind == "shot":
                    yield {"data": json.dumps({"step": "shot", "url": item["url"]})}

                elif kind == "question":
                    yield {"data": json.dumps({"step": "question", "msg": item["msg"], "shot": item.get("shot", "")})}

                elif kind == "error":
                    msg = item["msg"]
                    if "playwright" in msg.lower():
                        yield {"data": json.dumps({"step": "apply_unavailable",
                            "msg": "Run: playwright install chromium to enable auto-apply."})}
                    else:
                        yield {"data": json.dumps({"step": "error", "msg": _friendly_error(msg)[:400]})}
                    break

                elif kind == "result":
                    result = item["result"]
                    status = "Submitted" if result.status == "success" else "Failed"
                    # getattr: result may be EasyApplyResult or ExternalApplyResult —
                    # a missing attribute must never crash the stream after a
                    # successful submit.
                    _job_title = getattr(result, "job_title", "") or _extract_title(result_data.get("job_description", ""))
                    _company   = getattr(result, "company", "")   or _extract_company(result_data.get("job_description", ""))
                    _log_application(session_id, {
                        "job_title":   _job_title,
                        "company":     _company,
                        "job_url":     job_url, "portal": result.portal,
                        "match_score": score,
                        "final_score": result_data.get("rewrite", {}).get("final_score_estimate", 0),
                        "resume_url":  result_data.get("pdf_url", ""), "status": status,
                    })
                    yield {"data": json.dumps({
                        "step": "applied", "status": result.status,
                        "company": _company, "job_title": _job_title,
                        "portal": result.portal, "fields_filled": result.fields_filled,
                        "fields_skipped": result.fields_skipped,
                        "error": _friendly_error(result.error or ""),
                        "notifications": notifications,
                        "msg": f"{'Submitted!' if result.status=='success' else 'Done'} — {result.portal}",
                    })}
                    break
        finally:
            APPLY_ANSWERS.pop(session_id, None)
            try:
                await worker
            except Exception:
                pass
            # The tailored PDF (tailored_tmp) is intentionally KEPT so the UI can show
            # it; the next run purges old _tailored_*.pdf, so only the latest survives.

    return EventSourceResponse(generate())


@app.post("/api/apply-answer")
async def apply_answer(body: dict):
    """Browser posts the user's answer to a stuck-question raised by auto-apply."""
    sid = body.get("session_id", "")
    ans = body.get("answer", "")
    q   = APPLY_ANSWERS.get(sid)
    if q is None:
        raise HTTPException(404, "No active application is waiting for an answer.")
    q.put(ans)
    return {"ok": True}


# ── LinkedIn cookie status + login ────────────────────────────────────────────
@app.get("/api/linkedin-status")
async def linkedin_status():
    cookies_file = Path("linkedin_cookies.json")
    if cookies_file.exists():
        try:
            cookies = json.loads(cookies_file.read_text(encoding="utf-8"))
            return {"logged_in": bool(cookies), "cookie_count": len(cookies)}
        except Exception:
            pass
    return {"logged_in": False, "cookie_count": 0}

@app.get("/api/linkedin-login")
async def linkedin_login_stream():
    """Launch LinkedIn browser login and stream progress."""

    async def generate() -> AsyncGenerator[dict, None]:
        yield {"data": json.dumps({"step": "launching",
            "msg": "Opening LinkedIn in a browser on this machine…"})}
        try:
            from linkedin_url_extractor import do_login
            email = os.getenv("LINKEDIN_EMAIL", "").strip()
            pw    = os.getenv("LINKEDIN_PASSWORD", "").strip()
            yield {"data": json.dumps({"step": "waiting",
                "msg": ("Browser open — it auto-fills your login; just solve any CAPTCHA/2FA. "
                        "It saves your session automatically when you're in.")})}
            ok = await asyncio.to_thread(do_login, email, pw)
            yield {"data": json.dumps({"step": "done" if ok else "error",
                "msg": ("✅ LinkedIn connected — session saved. You can apply to LinkedIn URLs now."
                        if ok else "Login wasn't completed in time. Click Connect LinkedIn and try again.")})}
        except (ImportError, AttributeError):
            yield {"data": json.dumps({"step": "info",
                "msg": "Run in your terminal:\n\nuv run python linkedin_url_extractor.py login\n\nThen refresh."})}
        except Exception as e:
            yield {"data": json.dumps({"step": "error", "msg": str(e)[:200]})}

    return EventSourceResponse(generate())


# ── Jobs ──────────────────────────────────────────────────────────────────────
@app.get("/api/job-meta")
async def job_meta():
    """Return location options and experience level options for the job feed UI."""
    return {
        "locations": list(LOCATIONS.keys()),
        "exp_levels": list(EXP_LEVELS.keys()),
        "time_filters": list(TIME_FILTERS.keys()),
    }


@app.get("/api/jobs")
async def get_jobs(
    role: str = "data scientist",
    time_filter: str = "r86400",
    limit: int = 25,
    # multi-select: comma-separated values from the pill UI
    locations: str = "Bengaluru",   # e.g. "Bengaluru,Hyderabad"
    exp_levels: str = "",           # e.g. "3,4"  (LinkedIn f_E values)
    big_tech_only: bool = False,
    # legacy single-value params (kept for backwards compat)
    location: str = "",
    exp_level: str = "",
):
    tf = TIME_FILTERS.get(time_filter, time_filter)
    # Resolve location list
    loc_list = [l.strip() for l in locations.split(",") if l.strip()]
    if not loc_list and location:
        loc_list = [location]
    if not loc_list:
        loc_list = ["Bengaluru"]
    # Resolve exp string (LinkedIn f_E accepts "3,4" natively)
    exp_str = exp_levels.strip() or exp_level.strip()
    try:
        jobs = await asyncio.to_thread(
            fetch_jobs, role,
            time_filter=tf, max_jobs=limit,
            locations=loc_list, exp_level=exp_str,
            big_tech_only=big_tech_only,
        )
        return [{"title": j.title, "company": j.company, "location": j.location,
                 "url": j.url, "posted": j.posted, "source": j.source,
                 "tags": j.tags} for j in jobs]
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.get("/api/dashboard")
async def get_dashboard(session_id: str):
    with get_db() as conn:
        apps = conn.execute(
            "SELECT * FROM applications WHERE session_id=? ORDER BY created_at DESC",
            (session_id,)).fetchall()
    total     = len(apps)
    submitted = sum(1 for a in apps if a["status"] == "Submitted")
    tailored  = sum(1 for a in apps if a["status"] == "Tailored")
    this_week = sum(1 for a in apps if a["created_at"] and a["created_at"][:10] >= _week_start())
    return {"stats": {"total": total, "submitted": submitted,
                      "tailored": tailored, "this_week": this_week},
            "applications": [dict(a) for a in apps]}

@app.patch("/api/application/{app_id}")
async def update_application(app_id: int, body: dict):
    allowed = {"status", "notes"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates: raise HTTPException(400, "Nothing to update")
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with get_db() as conn:
        conn.execute(f"UPDATE applications SET {set_clause} WHERE id=?",
                     list(updates.values()) + [app_id])
    return {"ok": True}

@app.delete("/api/application/{app_id}")
async def delete_application(app_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
    return {"ok": True}

@app.post("/api/application")
async def add_application(body: dict):
    _log_application(body.get("session_id", "manual"), body)
    return {"ok": True}


# ── Session check ────────────────────────────────────────────────────────────
@app.get("/api/session/{session_id}")
async def check_session(session_id: str):
    session = SESSIONS.get(session_id)
    if not session:
        return {"valid": False}
    return {"valid": True, "name": session.get("name", ""), "filename": session.get("filename", "")}


# ── Profile ───────────────────────────────────────────────────────────────────
@app.get("/api/profile/{session_id}")
async def get_profile(session_id: str):
    return _get_profile(session_id)

@app.post("/api/profile/{session_id}")
async def save_profile_endpoint(session_id: str, body: dict):
    _save_profile(session_id, body)
    return {"ok": True}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _log_application(session_id: str, data: dict):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO applications
              (session_id,job_title,company,job_url,portal,applied_on,
               status,match_score,final_score,resume_url,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (session_id, data.get("job_title",""), data.get("company",""),
              data.get("job_url",""), data.get("portal",""),
              data.get("applied_on", datetime.now().strftime("%Y-%m-%d")),
              data.get("status","Applied"), data.get("match_score",0),
              data.get("final_score",0), data.get("resume_url",""), data.get("notes","")))

def _get_profile(session_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else {}

def _save_profile(session_id: str, data: dict):
    fields = ["full_name","email","phone","linkedin","github","city",
              "current_title","years_experience","notice_period","expected_ctc","current_ctc"]
    with get_db() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO profiles (session_id, {', '.join(fields)}) VALUES (?, {', '.join('?'*len(fields))})",
            [session_id] + [data.get(f,"") for f in fields])

def _enrich_apply_profile(extracted: dict, resume_text: str = ""):
    """Save the FULL resume-extracted profile (incl. education) into the stable
    apply profile so the agent has it as structured data and never re-asks for
    what the resume already contains. Only fills empty fields (never overwrites)."""
    try:
        from profile_manager import load_profile, save_profile
        p = load_profile(APPLY_USER_ID)
        for k, v in (extracted or {}).items():
            if v and not str(p.get(k) or "").strip():
                p[k] = v
        if p.get("full_name") and not p.get("first_name"):
            parts = str(p["full_name"]).split()
            p["first_name"] = parts[0] if parts else ""
            p["last_name"]  = " ".join(parts[1:]) if len(parts) > 1 else ""
        if resume_text:
            p["_resume_text"] = resume_text
        save_profile(APPLY_USER_ID, p)
    except Exception:
        pass


def _sync_profile_to_manager(user_id: int, profile: dict, resume_text: str = ""):
    try:
        from profile_manager import load_profile, save_profile
        existing = load_profile(user_id)
        for k in ["full_name","linkedin","github","city","current_title","years_experience","notice_period","expected_ctc","current_ctc"]:
            if profile.get(k): existing[k] = profile[k]
        if profile.get("full_name"):
            parts = profile["full_name"].split()
            existing["first_name"] = parts[0] if parts else ""
            existing["last_name"]  = " ".join(parts[1:]) if len(parts) > 1 else ""
        if resume_text:
            existing["_resume_text"] = resume_text
        # Self-enrich from the resume if structured fields (e.g. education) are still
        # missing — so the agent has them without the user re-uploading or being asked.
        rt = existing.get("_resume_text", "")
        if rt and not (str(existing.get("degree") or "").strip()
                       and str(existing.get("university") or "").strip()):
            try:
                ext = call_llm(build_profile_extract_prompt(rt))
                for k, v in (ext or {}).items():
                    if v and not str(existing.get(k) or "").strip():
                        existing[k] = v
            except Exception:
                pass
        save_profile(user_id, existing)
    except Exception:
        pass

def _extract_title(text: str) -> str:
    for line in text.splitlines():
        if any(w in line.lower() for w in ["engineer","scientist","analyst","manager","developer","architect"]):
            return line.strip()[:80]
    return "Software Role"

def _extract_company(text: str) -> str:
    for line in text.splitlines()[:10]:
        line = line.strip()
        if line and len(line) < 60 and line[0].isupper():
            return line
    return "Company"

def _week_start() -> str:
    today = date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
