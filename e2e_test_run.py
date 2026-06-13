"""One-shot end-to-end test of the external apply engine against a real URL.
Runs the same orchestrator path the web app uses, with console callbacks so
the whole run is observable from the terminal. on_stuck logs the question and
returns no guidance — i.e. fully autonomous behavior, questions made visible.

Usage: uv run python e2e_test_run.py <job_url>
"""

import os
import sys
import asyncio
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from dotenv import load_dotenv
load_dotenv()

JOB_URL = sys.argv[1] if len(sys.argv) > 1 else ""
if not JOB_URL:
    print("usage: python e2e_test_run.py <job_url>")
    sys.exit(1)


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def on_notify(msg):
    print(f"{ts()} [NOTIFY] {msg}", flush=True)


async def on_screenshot(path):
    print(f"{ts()} [SHOT] {path}", flush=True)


async def on_stuck(question):
    print(f"{ts()} [ASK-USER] {question}", flush=True)
    return ""  # no human present — log the ask, give no guidance


async def main():
    from google import genai
    from apply_orchestrator import run_application

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""),
                          http_options={"timeout": 120_000})

    print(f"{ts()} E2E TEST → {JOB_URL}", flush=True)
    res = await run_application(
        job_url=JOB_URL,
        resume_path="samples/demo_resume.pdf",
        user_id=1,
        gemini_client=client,
        model="gemini-3.5-flash",
        pro_model="gemini-3.5-flash",
        auto_submit=True,
        on_notify=on_notify,
        on_stuck=on_stuck,
        on_screenshot=on_screenshot,
    )

    print(f"\n{ts()} ════ RESULT ════", flush=True)
    print(f"  status : {res.status}", flush=True)
    print(f"  portal : {res.portal}", flush=True)
    print(f"  error  : {res.error or '—'}", flush=True)
    print(f"  filled : {len(res.fields_filled)} fields", flush=True)
    for f in res.fields_filled[:25]:
        print(f"    ✓ {f}", flush=True)
    print(f"  steps  : {res.steps_completed}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
