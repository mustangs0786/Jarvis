"""
job_wiki.py — Per-portal knowledge store
=========================================
Remembers what we learned about each job portal (keyed by domain) so future
applications don't repeat work. Most importantly: whether we already created an
account there, so we log in instead of trying to register again.

Stored in job_wiki.json at the project root:
  {
    "accenture.com": {
      "domain": "accenture.com",
      "account_created": true,
      "email": "you@example.com",
      "path_used": "register",
      "portal_notes": "...",
      "updated_at": "2026-05-23T..."
    }
  }

Passwords are never stored here — they live in .env (APPLY_PASSWORD).
"""

import json
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

WIKI_PATH = Path("job_wiki.json")


# Subdomain labels job portals commonly vary (www.acme.com vs jobs.acme.com vs
# careers.acme.com) — strip them so the same portal collapses to one key.
_STRIP_SUBDOMAINS = {"www", "jobs", "job", "careers", "career", "apply",
                     "boards", "recruiting", "talent", "hire", "work"}

def domain_of(url: str) -> str:
    try:
        net = urlparse(url).netloc.lower().split(":")[0]
        labels = net.split(".")
        # Drop a single leading portal-style subdomain (e.g. jobs., careers.)
        while len(labels) > 2 and labels[0] in _STRIP_SUBDOMAINS:
            labels = labels[1:]
        return ".".join(labels)
    except Exception:
        return ""


def load_wiki() -> dict:
    if WIKI_PATH.exists():
        try:
            return json.loads(WIKI_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def get_portal_knowledge(url: str) -> dict:
    """Return what we know about the portal serving this URL ({} if nothing)."""
    return load_wiki().get(domain_of(url), {})


def save_portal_knowledge(url: str, data: dict) -> dict:
    """Merge new facts about a portal into the wiki. Never stores passwords."""
    wiki = load_wiki()
    dom = domain_of(url)
    if not dom:
        return wiki
    entry = wiki.get(dom, {})
    entry.update({k: v for k, v in data.items() if k != "password"})
    entry["domain"] = dom
    entry["updated_at"] = datetime.now().isoformat()
    wiki[dom] = entry
    WIKI_PATH.write_text(json.dumps(wiki, indent=2, ensure_ascii=False), encoding="utf-8")
    return wiki


# ── Cross-portal lessons ─────────────────────────────────────────────────────
# Many portals share an ATS "type" (Workday, Phenom, SmartRecruiters...). Lessons
# learned on one transfer to a NEW-but-similar one. We store distilled tips
# (the resolutions, not raw logs) keyed by type under a reserved wiki key.
_LESSONS_KEY = "__lessons__"

def portal_type(url: str) -> str:
    u = (url or "").lower()
    if "myworkdayjobs.com" in u or ".wd" in u:        return "workday"
    if "phenom" in u or "jobs-ta." in u:               return "phenom"
    if "smartrecruiters" in u:                          return "smartrecruiters"
    if "icims" in u:                                    return "icims"
    if "greenhouse" in u:                               return "greenhouse"
    if "lever.co" in u:                                 return "lever"
    if "b2clogin" in u or "successfactors" in u:        return "sap/b2c"
    if "oraclecloud" in u or "taleo" in u:              return "oracle/taleo"
    return "generic"

def get_lessons(url: str) -> list:
    """Distilled tips learned on portals of the SAME type as this URL."""
    return (load_wiki().get(_LESSONS_KEY, {}) or {}).get(portal_type(url), [])

def add_lessons(url: str, tips: list):
    """Record distilled tips (e.g. user resolutions) for this portal type."""
    tips = [t.strip() for t in (tips or []) if t and t.strip()]
    if not tips:
        return
    wiki = load_wiki()
    box  = wiki.setdefault(_LESSONS_KEY, {})
    t    = portal_type(url)
    cur  = box.get(t, [])
    for tip in tips:
        if tip not in cur:
            cur.append(tip)
    box[t] = cur[-15:]   # keep it small
    WIKI_PATH.write_text(json.dumps(wiki, indent=2, ensure_ascii=False), encoding="utf-8")
