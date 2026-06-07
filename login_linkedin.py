#!/usr/bin/env python3
"""
login_linkedin.py — one-time LinkedIn sign-in that SAVES your session.

Reads LINKEDIN_EMAIL / LINKEDIN_PASSWORD from .env, opens Chrome, auto-fills and
signs in. If LinkedIn shows a CAPTCHA / 2FA, just solve it in the window (it waits
up to 3 minutes). On success it writes linkedin_cookies.json, which every later
agent run reuses — so you never hit the login wall again.

Run once:
    uv run python login_linkedin.py
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    email    = os.getenv("LINKEDIN_EMAIL", "").strip()
    password = os.getenv("LINKEDIN_PASSWORD", "").strip()
    if not email or not password:
        print("❌ Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD in your .env first.")
        sys.exit(1)

    # Reuse the tested login (best-effort auto-fill + wait for you to finish any
    # CAPTCHA/2FA + saves cookies in the format the agent loads).
    from linkedin_url_extractor import do_login

    print(f"🔐 Opening Chrome and signing in as {email} …")
    print("   → If a CAPTCHA / 2FA appears, just solve it in the window (up to 3 min).")
    ok = do_login(email, password)
    if ok:
        print("✅ Signed in — session saved to linkedin_cookies.json. You're set; "
              "the agent will reuse it on every run.")
    else:
        print("❌ Login didn't complete in time. Re-run and finish in the browser window.")
        sys.exit(1)


if __name__ == "__main__":
    main()
