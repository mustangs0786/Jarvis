"""
test_easy_apply.py — Run this locally to debug Easy Apply for a specific job URL.

Usage:
    python test_easy_apply.py

Requirements:
    - .env with LINKEDIN_EMAIL, LINKEDIN_PASSWORD, GEMINI_API_KEY
    - linkedin_cookies.json (run: python linkedin_url_extractor.py login)
    - pip install playwright google-genai python-dotenv
    - playwright install chromium
"""

import asyncio
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)

JOB_URL    = "https://in.linkedin.com/jobs/view/data-scientist-at-flipkart-4387960445"
USER_ID    = 12345  # dummy for local test
RESUME_PATH = ""    # set to your PDF path e.g. "output/resume.pdf"

async def main():
    from google import genai
    from linkedin_url_extractor import resolve_job_url
    from linkedin_easy_apply import run_easy_apply

    gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    model = "gemini-3.5-flash"

    # Find resume
    resume = RESUME_PATH
    if not resume:
        pdfs = sorted(Path("user_profiles").rglob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)
        resume = str(pdfs[0]) if pdfs else ""
    print(f"\n📄 Resume: {resume or '❌ NOT FOUND'}")
    if not resume:
        print("Set RESUME_PATH at top of script or add a PDF to user_profiles/")
        return

    print(f"\n🔍 Resolving URL: {JOB_URL}")
    resolved = await resolve_job_url(JOB_URL, gemini_client, model)
    print(f"   Job:       {resolved.get('job_title')} at {resolved.get('company')}")
    print(f"   Easy Apply:{resolved.get('easy_apply')}")
    print(f"   Apply URL: {resolved.get('apply_url') or '(none — Easy Apply)'}")

    if not resolved.get("easy_apply") and not resolved.get("apply_url"):
        print("❌ Could not resolve URL")
        return

    if resolved.get("easy_apply"):
        print("\n⚡ Starting Easy Apply...")

        async def on_notify(msg): print(f"  📢 {msg}")
        async def on_stuck(q):
            print(f"\n  ❓ STUCK: {q}")
            return input("  Your answer: ").strip()
        async def on_screenshot(path):
            print(f"  📸 Screenshot: {path}")

        result = await run_easy_apply(
            job_url       = JOB_URL,
            resume_path   = resume,
            user_id       = USER_ID,
            gemini_client = gemini_client,
            model         = model,
            on_stuck      = on_stuck,
            on_screenshot = on_screenshot,
            on_notify     = on_notify,
        )

        print(f"\n{'='*50}")
        print(f"Status:         {result.status}")
        print(f"Steps completed:{result.steps_completed}")
        print(f"Fields filled:  {result.fields_filled}")
        print(f"Fields skipped: {result.fields_skipped}")
        print(f"Error:          {result.error or 'none'}")
        print(f"Screenshot:     {result.screenshot_path}")
    else:
        print(f"\n🌐 External portal: {resolved['apply_url']}")
        print("Run apply_handler directly for external portals.")

if __name__ == "__main__":
    Path("output").mkdir(exist_ok=True)
    asyncio.run(main())