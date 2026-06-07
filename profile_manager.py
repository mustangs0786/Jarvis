"""
profile_manager.py — Per-user profile storage + self-learning
=============================================================
Storage per Telegram user_id:
  user_profiles/<user_id>/
    profile.json      ← grows automatically with every new answer
    apply_log.json    ← every application attempt

.env variables:
  APPLY_EMAIL=youremail@gmail.com
  APPLY_PASSWORD=YourPassword@123
"""

import os
import json
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

PROFILES_DIR = Path("user_profiles")
PROFILES_DIR.mkdir(exist_ok=True)

PROFILE_SKELETON = {
    "full_name": "", "first_name": "", "last_name": "",
    "phone": "", "phone_country_code": "+91", "location": "", "city": "", "country": "India",
    "linkedin": "", "portfolio": "", "github": "",
    "current_company": "", "current_title": "", "years_experience": "",
    "notice_period": "", "expected_ctc": "", "current_ctc": "",
    "willing_to_relocate": "", "work_authorization": "",
    "gender": "", "graduation_year": "", "degree": "",
    "university": "", "cgpa": "",
    "screening": {}   # learned answers: {question_lower: answer}
}

FIELD_MAP = {
    "full name": "full_name", "name": "full_name",
    "first name": "first_name", "given name": "first_name",
    "last name": "last_name", "surname": "last_name", "family name": "last_name",
    "email": "email", "email address": "email", "email id": "email",
    "phone": "phone", "phone number": "phone", "mobile": "phone",
    "mobile number": "phone", "contact number": "phone",
    "mobile phone number": "phone", "phone country code": "phone_country_code",
    "mobile country code": "phone_country_code", "country code": "phone_country_code",
    "phone country code": "phone_country_code",
    "country phone code": "phone_country_code",
    "country territory phone code": "phone_country_code",
    "phone extension": "phone_extension", "extension": "phone_extension",
    "phone device type": "phone_device_type", "device type": "phone_device_type",
    "location": "location", "city": "city", "country": "country", "address": "location",
    "address line 1": "street_address", "address line": "street_address",
    "street address": "street_address", "street": "street_address",
    "address line 2": "address_line_2", "apartment": "address_line_2",
    "postal code": "postal_code", "post code": "postal_code", "zip": "postal_code",
    "zip code": "postal_code", "pin code": "postal_code", "pincode": "postal_code",
    "state": "state", "province": "state", "region": "state",
    "linkedin": "linkedin", "linkedin url": "linkedin", "linkedin profile": "linkedin",
    "portfolio": "portfolio", "website": "portfolio", "personal website": "portfolio",
    "github": "github", "github url": "github",
    "current company": "current_company", "current employer": "current_company",
    "company name": "current_company", "employer": "current_company",
    "current role": "current_title", "current title": "current_title",
    "job title": "current_title", "designation": "current_title",
    "years of experience": "years_experience", "total experience": "years_experience",
    "experience": "years_experience",
    "notice period": "notice_period", "notice": "notice_period",
    "expected ctc": "expected_ctc", "expected salary": "expected_ctc",
    "current ctc": "current_ctc", "current salary": "current_ctc",
    "willing to relocate": "willing_to_relocate", "open to relocation": "willing_to_relocate",
    "work authorization": "work_authorization", "authorized to work": "work_authorization",
    "gender": "gender",
    "graduation year": "graduation_year", "year of graduation": "graduation_year",
    "degree": "degree", "highest qualification": "degree",
    "university": "university", "college": "university", "institution": "university",
    "cgpa": "cgpa", "gpa": "cgpa", "percentage": "cgpa",
}

IMPORTANT_FIELDS = [
    "full_name", "phone", "city", "linkedin",
    "current_title", "years_experience", "notice_period",
    "expected_ctc", "current_ctc",
]


def get_user_dir(user_id: int) -> Path:
    d = PROFILES_DIR / str(user_id)
    d.mkdir(exist_ok=True)
    return d

def load_profile(user_id: int) -> dict:
    path = get_user_dir(user_id) / "profile.json"
    stored = {}
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    profile = {**PROFILE_SKELETON, **stored}
    profile["screening"] = {**PROFILE_SKELETON["screening"], **stored.get("screening", {})}
    # Always pull from .env — never stored on disk
    profile["email"]    = os.getenv("APPLY_EMAIL", stored.get("email", ""))
    profile["password"] = os.getenv("APPLY_PASSWORD", stored.get("password", ""))
    return profile

def save_profile(user_id: int, profile: dict):
    """Persist profile. email/password never stored — they stay in .env."""
    to_store = {k: v for k, v in profile.items() if k not in ("email", "password")}
    path = get_user_dir(user_id) / "profile.json"
    path.write_text(json.dumps(to_store, indent=2, ensure_ascii=False), encoding="utf-8")

def profile_exists(user_id: int) -> bool:
    path = get_user_dir(user_id) / "profile.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        return bool(data.get("full_name") or data.get("phone"))
    except Exception:
        return False

def learn_answer(user_id: int, question: str, answer: str) -> dict:
    """
    Save a new answer to screening dict. Called every time user answers
    an unknown field during application. Immediately persisted.
    Returns updated profile.
    """
    profile = load_profile(user_id)
    profile["screening"][question.lower().strip()] = answer
    save_profile(user_id, profile)
    return profile

def update_field(user_id: int, key: str, value: str):
    """Update a single top-level profile field and persist."""
    profile = load_profile(user_id)
    profile[key] = value
    save_profile(user_id, profile)

_FIELD_TOKEN_RE = __import__("re").compile(r"[a-z0-9]+")
# Stopwords that don't discriminate between fields. Used so labels like
# "Please enter your phone number" still match the "phone number" mapping.
# Carefully chosen NOT to include words that appear in real mapping keys
# (e.g. "name", "address", "url" are NOT noise — they discriminate).
_FIELD_TOKEN_NOISE = {
    "the", "a", "an", "your", "my", "our", "this", "that",
    "please", "enter", "type", "select", "choose", "field",
    "required", "optional", "must", "have", "be", "is", "of",
    "info", "information", "details",
}

def _field_tokens(s: str) -> set:
    """Lowercase whole-word tokens with stopwords removed."""
    return set(_FIELD_TOKEN_RE.findall(s.lower())) - _FIELD_TOKEN_NOISE


# ── Section-aware row lookup ─────────────────────────────────────────────────
# Maps the visible section header word to the profile list name.
_SECTION_KEY_MAP = {
    "work experience":  "experience",
    "employment":       "experience",
    "education":        "education",
    "certification":    "certifications",
    "certifications":   "certifications",
    "language":         "languages",
    "languages":        "languages",
    "project":          "projects",
    "projects":         "projects",
    "award":            "awards",
    "awards":           "awards",
}

# Aliases within an experience/education entry — used so a label like
# "Role Description*" matches the entry's "description" key, "Currently work
# here" matches "is_current", "Overall Result (GPA)" matches "gpa", etc.
_ROW_FIELD_ALIASES = {
    # experience
    "title":          ("job title", "title", "role", "position", "designation"),
    "company":        ("company", "employer", "organization"),
    "location":       ("location", "city", "place"),
    "start_date":     ("from", "start date", "start", "start year"),
    "end_date":       ("to", "end date", "end", "end year"),
    "description":    ("role description", "description", "responsibilities", "summary", "details"),
    "is_current":     ("currently work", "current", "present", "i currently work here"),
    # education
    "institution":    ("school or university", "school", "university", "college", "institution"),
    "degree":         ("degree",),
    "field_of_study": ("field of study", "major", "specialization"),
    "start_year":     ("start year", "from year"),
    "end_year":       ("end year", "to year", "graduation year", "year of graduation"),
    "gpa":            ("gpa", "cgpa", "overall result", "result", "percentage"),
    # certifications
    "name":           ("certification name", "name", "certificate name"),
    "issuer":         ("issuer", "issuing organization", "authority"),
    "date":           ("date", "issue date", "issued"),
}

_SECTION_RE = __import__("re").compile(
    r"(Work Experience|Employment|Education|Certifications?|Languages?|Projects?|Awards?)\s+(\d+)",
    __import__("re").I,
)

def _parse_section_label(section_label: str):
    """'Work Experience 2' -> ('experience', 1). Returns None if no match."""
    if not section_label:
        return None
    m = _SECTION_RE.search(section_label)
    if not m:
        return None
    word = m.group(1).lower()
    list_name = _SECTION_KEY_MAP.get(word) or _SECTION_KEY_MAP.get(word.rstrip("s"))
    if not list_name:
        return None
    return (list_name, int(m.group(2)) - 1)

def _label_to_subkey(label: str, entry_keys) -> str | None:
    """Find which key inside a row entry the label maps to.

    Tries:
      a) whole-token match of the label against the entry's own keys
         (`description` matches "Role Description*", etc.)
      b) the _ROW_FIELD_ALIASES table — alias whose tokens fit the label,
         then return the canonical sub-key if that key exists in the entry.
    """
    label_tokens = _field_tokens(label)
    if not label_tokens:
        return None
    entry_keys = list(entry_keys or [])

    # (a) Direct token match against the row entry's own keys.
    best = None
    for k in entry_keys:
        k_tokens = _field_tokens(str(k).replace("_", " "))
        if k_tokens and k_tokens.issubset(label_tokens):
            spec = (len(k_tokens), len(str(k)))
            if best is None or spec > best[0]:
                best = (spec, k)
    if best:
        return best[1]

    # (b) Alias table — pick the canonical key whose alias tokens fit the label.
    best_alias = None  # (specificity, canonical_key)
    for canon, aliases in _ROW_FIELD_ALIASES.items():
        for alias in aliases:
            a_tokens = _field_tokens(alias.replace("_", " "))
            if a_tokens and a_tokens.issubset(label_tokens):
                spec = (len(a_tokens), len(alias))
                if best_alias is None or spec > best_alias[0]:
                    best_alias = (spec, canon)
                break
    if best_alias:
        canon = best_alias[1]
        # Honor the entry's actual key name if it exists, otherwise canon.
        if canon in entry_keys:
            return canon
        for a in _ROW_FIELD_ALIASES.get(canon, ()):
            ak = a.replace(" ", "_")
            if ak in entry_keys:
                return ak
        return canon if canon in entry_keys else None
    return None


def get_field_value(label: str, profile: dict, section_label: str | None = None) -> str | None:
    """Return profile value for a form field label. None = must ask user.

    If `section_label` is provided (e.g. "Work Experience 2"), tries the
    multi-row tier first: profile.<list>[N-1].<sub_key>. Falls through to
    the existing flat-profile logic if the section lookup misses, so
    single-row pages keep working unchanged.
    """
    # ─── Section-aware tier (multi-row pages) ──────────────────────────
    # Skip this tier for question-like labels (screening sentences such as
    # "Would you be willing to share your LinkedIn profile with us?"). Generic
    # single-word row aliases like "to"/"from" would otherwise falsely match a
    # date sub-key (end_date) inside such a sentence. A real field label is
    # short ("From", "Company*", "School or University*"); a question is long
    # and/or ends in "?".
    _looks_like_question = ("?" in (label or "")) or (len((label or "").split()) > 5)
    if section_label and not _looks_like_question:
        parsed = _parse_section_label(section_label)
        if parsed:
            list_name, row_idx = parsed
            entries = profile.get(list_name) or []
            if isinstance(entries, list) and 0 <= row_idx < len(entries):
                entry = entries[row_idx] or {}
                sub_key = _label_to_subkey(label, entry.keys())
                if sub_key:
                    val = entry.get(sub_key)
                    if val not in (None, ""):
                        return str(val)
                    # is_current is a bool — convert to str the executor accepts.
                    if isinstance(val, bool):
                        return "true" if val else "false"
    # ─── Flat-profile tier (existing logic, unchanged) ─────────────────
    key = label.lower().strip()
    # 1) Exact match wins.
    if key in FIELD_MAP:
        val = profile.get(FIELD_MAP[key], "")
        if val: return str(val)
    # 2) Whole-token subset: every word in the mapping key must appear as a
    #    whole word in the label. Most-specific (longest) mapping wins so
    #    "linkedin profile" beats "linkedin" when both could apply.
    label_tokens = _field_tokens(label)
    if label_tokens:
        best = None  # (specificity, profile_key)
        for mk, pk in FIELD_MAP.items():
            mk_tokens = _field_tokens(mk)
            if not mk_tokens:
                continue
            if mk_tokens.issubset(label_tokens):
                # Score: token-count first, then mapping-key string length
                # (tie-breaker — longer key is more discriminating, so
                # "college" beats "name" for "College Name*").
                spec = (len(mk_tokens), len(mk))
                if best is None or spec > best[0]:
                    best = (spec, pk)
        if best:
            val = profile.get(best[1], "")
            if val: return str(val)
    # 3) Screening dict — same whole-token subset rule (was bidirectional
    #    substring which leaked across unrelated questions).
    if label_tokens:
        for q, a in profile.get("screening", {}).items():
            q_tokens = _field_tokens(q)
            if q_tokens and q_tokens.issubset(label_tokens):
                return str(a)
    return None

def get_missing_fields(profile: dict) -> list:
    return [f for f in IMPORTANT_FIELDS if not profile.get(f)]

def profile_completeness(profile: dict) -> int:
    filled = sum(1 for f in IMPORTANT_FIELDS if profile.get(f))
    return int(filled / len(IMPORTANT_FIELDS) * 100)


# ── Apply log ─────────────────────────────────────────────────────────────────

def log_application(user_id: int, entry: dict):
    path = get_user_dir(user_id) / "apply_log.json"
    log = []
    if path.exists():
        try: log = json.loads(path.read_text(encoding="utf-8"))
        except Exception: pass
    entry.setdefault("timestamp", datetime.now().isoformat())
    log.append(entry)
    path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")

def get_apply_stats(user_id: int) -> dict:
    path = get_user_dir(user_id) / "apply_log.json"
    if not path.exists(): return {"total": 0}
    try: log = json.loads(path.read_text(encoding="utf-8"))
    except Exception: return {"total": 0}
    total     = len(log)
    submitted = sum(1 for e in log if e.get("status") == "success")
    failed    = sum(1 for e in log if e.get("status") == "failed")
    portals   = {}
    for e in log:
        p = e.get("portal", "unknown")
        portals[p] = portals.get(p, 0) + 1
    last = log[-1] if log else {}
    return {
        "total": total, "submitted": submitted, "failed": failed,
        "portals": portals,
        "last_applied": last.get("timestamp", ""),
        "last_company": last.get("company", ""),
    }


# ── Extract profile from resume text ─────────────────────────────────────────

def build_extract_prompt(resume_text: str) -> str:
    return f"""Extract candidate details from this resume. Return ONLY JSON, no markdown.

{{
  "full_name": "", "first_name": "", "last_name": "",
  "phone": "", "city": "", "linkedin": "", "portfolio": "", "github": "",
  "current_company": "", "current_title": "", "years_experience": "",
  "graduation_year": "", "degree": "", "university": "", "cgpa": ""
}}

Resume:
{resume_text[:3000]}"""

def merge_resume_into_profile(user_id: int, resume_text: str, gemini_client, model: str) -> list:
    """
    Extract fields from resume text → merge into profile (only fills empty fields).
    Returns list of newly filled field names.
    """
    try:
        from google.genai import types as gt
        r = gemini_client.models.generate_content(
            model=model,
            contents=build_extract_prompt(resume_text),
            config=gt.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json"
            )
        )
        raw = r.text.strip().replace("```json","").replace("```","").strip()
        extracted = json.loads(raw)
    except Exception as e:
        print(f"  Profile extract failed: {e}")
        return []

    profile = load_profile(user_id)
    newly_filled = []
    for key, value in extracted.items():
        if value and not profile.get(key):
            profile[key] = value
            newly_filled.append(key)

    # Auto-split full_name → first/last
    if profile.get("full_name") and not profile.get("first_name"):
        parts = profile["full_name"].strip().split()
        if len(parts) >= 2:
            profile["first_name"] = parts[0]
            profile["last_name"]  = " ".join(parts[1:])
            if "first_name" not in newly_filled: newly_filled.append("first_name")
            if "last_name"  not in newly_filled: newly_filled.append("last_name")

    if newly_filled:
        save_profile(user_id, profile)
    return newly_filled
