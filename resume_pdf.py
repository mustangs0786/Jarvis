"""
Resume PDF Generator v2 — Adaptive, LaTeX-style, ATS-friendly
==============================================================
Install:  pip install fpdf2

Font setup:
  Download DejaVu fonts → https://dejavu-fonts.github.io/
  Place in fonts/ttf/ OR update FONTS_DIR below.

LLM Markdown format:
─────────────────────────────────────────
# Full Name
City, State
📞 Phone | ✉ email | linkedin | portfolio

## EXPERIENCE
**Company - Role Title | Date Range**
- Bullet with optional **inline bold** or *italic*

## PROJECTS
**Project Name | Tech Stack | [Link Text](url) | Date**
- Bullet

## EDUCATION
**University Name | Degree - CGPA: X.X | Date Range | City**

## TECHNICAL SKILLS
**Category:** item1, item2, item3

## PATENTS
**Patent Title | Filing Date**
- Description bullet

## COURSEWORK
Data Analysis | Machine Learning | SQL | Power BI

## CERTIFICATIONS
- Cert Name - Issuer | [View](url)
─────────────────────────────────────────
Any ## SECTION NAME works — the renderer adapts automatically.
"""

import re
from pathlib import Path
from fpdf import FPDF


# ── CONFIGURATION ─────────────────────────────────────────────────────────────

FONTS_DIR = Path(__file__).parent / "fonts"

# Sections that render as multi-column tag grid
# Sections rendered as vertical bullet list (one item per line, full width)
LIST_SECTIONS = {"certifications", "extracurricular / certifications",
                 "certifications & awards", "awards", "patents & awards",
                 "achievements", "honors", "extracurricular"}

GRID_SECTIONS = {"coursework", "relevant coursework",
                 "extracurricular", "extracurricular / certifications",
                 "certifications & awards", "awards", "patents & awards",
                 "achievements", "honors"}

# Summary section — plain flowing text
SUMMARY_SECTIONS = {"summary", "profile summary", "professional summary",
                    "career summary", "about", "objective"}

# Sections that render skills as "Bold Label: items" rows
SKILLS_SECTIONS = {"technical skills", "skills", "core skills", "tools"}

# Bare section names the LLM may output WITHOUT "## " prefix — fallback detection
# (all lower-cased for matching)
KNOWN_BARE_SECTIONS = {
    "summary", "profile summary", "professional summary", "career summary",
    "about", "objective",
    "experience", "work experience", "professional experience", "employment",
    "skills", "technical skills", "core skills", "tools", "technologies",
    "projects", "key projects", "personal projects", "academic projects",
    "education", "academic background", "qualifications",
    "achievements", "accomplishments", "awards", "honors",
    "certifications", "certifications & awards", "licenses",
    "patents", "patents & awards", "publications",
    "coursework", "relevant coursework",
    "extracurricular", "activities", "volunteer",
    "languages", "interests", "hobbies",
}

# Page geometry
ML, MR, MT, MB = 18, 18, 14, 18   # margins: left, right, top, bottom
LH = 5.2                            # base line height
BULLET_INDENT = 5
GRID_COLS = 4                       # columns for grid sections


# ── HELPERS ───────────────────────────────────────────────────────────────────

def sanitize(text: str) -> str:
    """
    Replace Unicode characters that Latin-1 / Helvetica can't encode.
    Keeps the resume looking correct while staying ATS-safe.
    """
    replacements = {
        '–': '-',   # en dash  →  hyphen
        '—': '-',   # em dash  →  hyphen
        '‘': "'",   # left single quote
        '’': "'",   # right single quote / apostrophe
        '“': '"',   # left double quote
        '”': '"',   # right double quote
        '•': '-',   # bullet • (we draw our own)
        'â': 'a',   # â
        'é': 'e',   # é
        'è': 'e',   # è
        'ó': 'o',   # ó
        '…': '...', # ellipsis
        '·': '-',   # middle dot
        '‒': '-',   # figure dash
        '―': '-',   # horizontal bar
        ' ': ' ',   # non-breaking space
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    # Final fallback: drop anything still outside Latin-1
    return text.encode('latin-1', errors='ignore').decode('latin-1')


def section_style(title: str) -> str:
    key = title.strip().lower()
    if key in SUMMARY_SECTIONS:
        return "summary"
    if key in LIST_SECTIONS:
        return "list"
    if key in GRID_SECTIONS:
        return "grid"
    if key in SKILLS_SECTIONS:
        return "skills"
    # Fuzzy match: any section containing "skill" → skills style
    if "skill" in key or "competenc" in key or "expertise" in key or "proficien" in key:
        return "skills"
    return "standard"


def strip_links(text: str) -> tuple[str, list[tuple]]:
    """
    Extract [label](url) markdown links.
    Returns (clean_text, [(label, url, char_pos), ...])
    """
    links = []
    def replace(m):
        links.append((m.group(1), m.group(2)))
        return m.group(1)
    clean = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', replace, text)
    return clean, links


def parse_inline(text: str) -> list[tuple[str, str]]:
    """
    Tokenize inline **bold** and *italic*.
    Returns list of (style, text): style in {"", "B", "I"}
    """
    tokens = re.split(r'(\*\*.*?\*\*|\*[^*]+?\*)', text)
    result = []
    for t in tokens:
        if not t:
            continue
        if t.startswith("**") and t.endswith("**"):
            result.append(("B", t[2:-2]))
        elif t.startswith("*") and t.endswith("*"):
            result.append(("I", t[1:-1]))
        else:
            result.append(("", t))
    return result


# ── VALIDATION LAYER ─────────────────────────────────────────────────────────
# Sits between LLM output and the parser/renderer.
# Catches and fixes the most common LLM formatting mistakes before they
# cause bad PDFs. Two functions:
#   validate_resume_md()   — fixes markdown text (pre-parse)
#   validate_parsed_doc()  — fixes parsed structure (post-parse)

import re as _re

# Month abbreviation map for date normalisation
_MONTH_MAP = {
    "january":"Jan","february":"Feb","march":"Mar","april":"Apr",
    "may":"May","june":"Jun","july":"Jul","august":"Aug",
    "september":"Sep","october":"Oct","november":"Nov","december":"Dec",
    "jan":"Jan","feb":"Feb","mar":"Mar","apr":"Apr",
    "jun":"Jun","jul":"Jul","aug":"Aug",
    "sep":"Sep","oct":"Oct","nov":"Nov","dec":"Dec",
}
_LOCATION_STRIP = _re.compile(
    r",[ \t]*(Bengaluru|Bangalore|Hyderabad|Chennai|Mumbai|Delhi|Pune|"
    r"Noida|Gurgaon|Gurugram|India|Remote|New York|San Francisco|London|"
    r"Singapore|US|USA|UK)[^|]*",
    _re.IGNORECASE
)


def _normalise_date_token(token: str) -> str:
    """Convert month names/numbers to 'Mon YYYY' format."""
    token = token.strip()
    # Already correct: "Dec 2022"
    if _re.match(r"[A-Z][a-z]{2}\s+\d{4}", token):
        return token
    # "December 2022" → "Dec 2022"
    m = _re.match(r"([A-Za-z]+)\s+(\d{4})", token)
    if m:
        mon = _MONTH_MAP.get(m.group(1).lower())
        return f"{mon} {m.group(2)}" if mon else token
    # "12/2022" or "12-2022" → "Dec 2022"
    m = _re.match(r"(\d{1,2})[/\-](\d{4})", token)
    if m:
        idx = int(m.group(1))
        months = ["Jan","Feb","Mar","Apr","May","Jun",
                  "Jul","Aug","Sep","Oct","Nov","Dec"]
        if 1 <= idx <= 12:
            return f"{months[idx-1]} {m.group(2)}"
    # "Present" / "Current" / "Now"
    if token.lower() in ("present","current","now","ongoing"):
        return "Present"
    return token


def _fix_date_range(date_str: str) -> str:
    """Normalise a date range like 'June 2022 - Dec 2022' → 'Jun 2022 - Dec 2022'."""
    if not date_str:
        return date_str
    # Split on dash variants
    parts = _re.split(r"\s*[–—\-]\s*", date_str, maxsplit=1)
    if len(parts) == 2:
        return f"{_normalise_date_token(parts[0])} - {_normalise_date_token(parts[1])}"
    return _normalise_date_token(date_str)


def validate_resume_md(md: str) -> tuple[str, list[str]]:
    """
    Fix common LLM formatting mistakes in raw markdown before parsing.
    Returns (fixed_markdown, list_of_fixes_applied).
    """
    fixes  = []
    lines  = md.splitlines()
    result = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Fix 0: Strip document title junk lines (RESUME, CURRICULUM VITAE) ─
        if _re.match(r"^(RESUME|CURRICULUM VITAE|CV)$", stripped, _re.IGNORECASE):
            fixes.append(f"Removed document title line: '{stripped}'")
            i += 1
            continue

        # ── Fix 1: Empty bold lines (**  ** or ****) → skip ──────────────────
        if _re.match(r"^\*{2,}\s*\*{2,}$", stripped):
            fixes.append("Removed empty bold line")
            i += 1
            continue

        # ── Fix 2: Bullet prefix * or • → - ──────────────────────────────────
        if _re.match(r"^\s*[*•]\s+\S", line) and not line.strip().startswith("**"):
            fixed = _re.sub(r"^(\s*)[*•]\s+", r"- ", line)
            if fixed != line:
                fixes.append(f"Fixed bullet prefix: '{line.strip()[:40]}'")
                line = fixed

        # ── Fix 3: Primary entry — detect split company/role across lines ─────
        # Pattern: "**Company**" alone, followed by "Role | Date" or italic line
        if (_re.match(r"^\*\*[^|*]+\*\*$", stripped) and
                not stripped.startswith("**##") and
                i + 1 < len(lines)):
            next_stripped = lines[i+1].strip()
            # Next line is a role/date line (not a bullet, not a section)
            if ("|" in next_stripped and
                    not next_stripped.startswith("**") and
                    not next_stripped.startswith("-") and
                    not next_stripped.startswith("#")):
                company = stripped.strip("*").strip()
                merged  = f"**{company} | {next_stripped}**"
                fixes.append(f"Merged split company line: '{company}'")
                result.append(merged)
                i += 2
                continue
            # Next line is italic role (no date yet)
            if (next_stripped.startswith("*") and next_stripped.endswith("*") and
                    not next_stripped.startswith("**")):
                company = stripped.strip("*").strip()
                role    = next_stripped.strip("*").strip()
                # Peek further for date line
                if (i + 2 < len(lines) and
                        _re.search(r"\d{4}", lines[i+2])):
                    date = lines[i+2].strip()
                    merged = f"**{company} | {role} | {_fix_date_range(date)}**"
                    fixes.append(f"Merged 3-line company entry: '{company}'")
                    result.append(merged)
                    i += 3
                    continue
                else:
                    merged = f"**{company} | {role}**"
                    fixes.append(f"Merged 2-line company entry: '{company}'")
                    result.append(merged)
                    i += 2
                    continue

        # ── Fix 4: Primary entry — strip location from company field ──────────
        if stripped.startswith("**") and stripped.endswith("**") and "|" in stripped:
            inner  = stripped[2:-2]
            parts  = [p.strip() for p in inner.split("|")]
            cleaned = []
            for p in parts:
                cleaned.append(_LOCATION_STRIP.sub("", p).strip(" ,"))
            # Remove empty parts created by stripping location
            cleaned = [p for p in cleaned if p]
            fixed_inner = " | ".join(cleaned)
            if fixed_inner != inner:
                fixes.append(f"Stripped location from entry: '{inner[:50]}'")
                line = f"**{fixed_inner}**"
            # Normalise dates inside the entry
            parts2 = [p.strip() for p in line[2:-2].split("|")]
            parts2 = [_fix_date_range(p) if _re.search(r"\d{4}", p) else p for p in parts2]
            line = f"**{' | '.join(parts2)}**"

        # ── Fix 5: Trailing asterisks on non-bold lines ───────────────────────
        if not stripped.startswith("**") and stripped.endswith("**"):
            fixed = line.rstrip().rstrip("*").rstrip()
            if fixed != line:
                fixes.append(f"Stripped trailing asterisks")
                line = fixed

        # ── Fix 6: Leading ** on skill items e.g. "** Python, SQL" ───────────
        if _re.match(r"^\*{1,2}\s+\w", stripped) and not _re.match(r"^\*\*[^*]+\*\*", stripped):
            fixed = _re.sub(r"^\*+\s*", "", stripped)
            fixes.append(f"Stripped leading asterisks from: '{stripped[:40]}'")
            line = fixed

        # ── Fix 7: Curly / smart quotes → straight quotes ─────────────────────
        for src, dst in [('"','"'),('"','"'),("'","'"),("`","'"),("'","'")]:
            if src in line:
                line = line.replace(src, dst)

        result.append(line)
        i += 1

    fixed_md = "\n".join(result)
    return fixed_md, fixes


def validate_parsed_doc(doc: dict) -> tuple[dict, list[str]]:
    """
    Validate and fix the parsed document structure.
    Returns (fixed_doc, list_of_warnings).
    """
    warnings = []

    # ── Name fallback ──────────────────────────────────────────────────────────
    if not doc.get("name"):
        doc["name"] = "Resume"
        warnings.append("Missing name — used fallback")

    # ── Contact line check ────────────────────────────────────────────────────
    if not doc.get("contact_lines"):
        warnings.append("No contact lines found")

    # ── Section-level fixes ───────────────────────────────────────────────────
    clean_sections = []
    for section in doc.get("sections", []):
        entries = section.get("entries", [])

        if not entries:
            warnings.append(f"Empty section skipped: {section['title']}")
            continue

        # Fix entries within section
        clean_entries = []
        seen_keys     = set()

        for e in entries:
            t = e.get("type")

            # Skip orphaned primaries with no company name
            if t == "primary" and not e.get("company","").strip():
                warnings.append("Skipped primary entry with no company name")
                continue

            # Deduplicate primary entries (same company + date)
            if t == "primary":
                key = (e.get("company",""), e.get("date",""))
                if key in seen_keys:
                    warnings.append(f"Duplicate entry removed: {key[0]}")
                    continue
                seen_keys.add(key)

            # Fix bullet date format in primary entries
            if t == "primary" and e.get("date"):
                e["date"] = _fix_date_range(e["date"])

            # Split overly long bullets at sentence boundary
            if t == "bullet":
                text = e.get("text","")
                if len(text) > 280:
                    # Try to split at ". " or ", " around midpoint
                    mid  = len(text) // 2
                    best = -1
                    for sep in [". ", ", "]:
                        idx = text.find(sep, mid - 40)
                        if idx != -1 and abs(idx - mid) < abs(best - mid):
                            best = idx + len(sep) - 1
                    if best > 0:
                        part1 = text[:best].strip()
                        part2 = text[best:].strip()
                        clean_entries.append({"type":"bullet","text":part1,"links":[]})
                        clean_entries.append({"type":"bullet","text":part2,"links":[]})
                        warnings.append(f"Split long bullet ({len(text)} chars)")
                        continue

            clean_entries.append(e)

        section["entries"] = clean_entries
        if clean_entries:
            clean_sections.append(section)

    doc["sections"] = clean_sections
    return doc, warnings


# ── PARSER ────────────────────────────────────────────────────────────────────


# Date patterns the LLM commonly outputs
DATE_RE = re.compile(
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}"
    r"|\d{4}\s*[-]\s*(?:\d{4}|Present|present|Current|current)"
    r"|Present|Current",
    re.IGNORECASE
)

# Location patterns to strip from company name
LOCATION_RE = re.compile(
    r',\s*(Bengaluru|Bangalore|Hyderabad|Chennai|Mumbai|Delhi|Pune|India|'
    r'Remote|New York|San Francisco|London|Singapore)[^|]*$',
    re.IGNORECASE
)

def _looks_like_date(s: str) -> bool:
    """Check if string looks like a date range."""
    return bool(DATE_RE.search(s))

def _looks_like_role(s: str) -> bool:
    """Check if string looks like a job role title."""
    role_keywords = [
        'engineer', 'scientist', 'analyst', 'developer', 'manager',
        'lead', 'architect', 'intern', 'consultant', 'director',
        'specialist', 'associate', 'senior', 'junior', 'staff', 'principal',
    ]
    return any(kw in s.lower() for kw in role_keywords)

def _extract_date(parts: list) -> str:
    """Find the date part among a list of pipe-separated parts."""
    for p in reversed(parts):
        if _looks_like_date(p):
            return p.strip()
    return ""

def _clean_company(name: str) -> str:
    """Strip location suffix from company name."""
    return LOCATION_RE.sub("", name).strip(" ,|")

def _parse_primary_entry(clean: str, links: list) -> dict:
    """
    Robustly parse a primary entry line into company/role/date.

    Handles all these LLM output variants:
      "Optum | Data Scientist | Dec 2022 - Present"      → 3 parts
      "Optum | Dec 2022 - Present"                       → 2 parts, no role
      "Optum | Bengaluru, India | Dec 2022 - Present"    → location in middle
      "Optum | Bengaluru"                                → no date (role comes next)
      "Optum - Data Scientist | Dec 2022 - Present"      → dash separator
    """
    # Normalise: split on pipe first
    parts = [p.strip() for p in clean.split("|")]

    company = parts[0].strip() if parts else clean
    role    = ""
    date    = ""

    if len(parts) == 1:
        # Could be "Company - Role - Date" with dashes
        dash_parts = [p.strip() for p in clean.split(" - ", 2)]
        if len(dash_parts) >= 3 and _looks_like_date(dash_parts[-1]):
            company = dash_parts[0]
            role    = dash_parts[1]
            date    = dash_parts[2]
        elif len(dash_parts) == 2:
            if _looks_like_date(dash_parts[-1]):
                company = dash_parts[0]
                date    = dash_parts[1]
            elif _looks_like_role(dash_parts[-1]):
                company = dash_parts[0]
                role    = dash_parts[1]
        # else: just a company name, role/date will come on next line

    elif len(parts) == 2:
        if _looks_like_date(parts[1]):
            # "Optum | Dec 2022 - Present"
            date = parts[1]
        elif _looks_like_role(parts[1]):
            # "Optum | Data Scientist" (date on next line)
            role = parts[1]
        else:
            # "Optum | Bengaluru, India" — location, ignore it
            pass  # role/date will be on next line

    elif len(parts) >= 3:
        date = _extract_date(parts)
        # Remaining non-date, non-location parts → role
        for p in parts[1:]:
            if p == date:
                continue
            if LOCATION_RE.search(p):
                continue  # skip location parts
            if p and not _looks_like_date(p):
                role = p
                break

    # Strip location from company name
    company = _clean_company(company)

    return {
        "type":    "primary",
        "company": company,
        "role":    role,
        "date":    date,
        "links":   links,
    }


def parse_resume(text: str) -> dict:
    """
    Parse LLM markdown into structured dict:
    {
      name, contact_lines: [str],
      sections: [{title, style, entries: [...]}]
    }
    Entry types:
      primary  → {company, role, date, links}
      bullet   → {text, links}
      skill    → {label, items}   (for skills sections)
      text     → {text}
      grid     → {items: [str]}   (for grid sections)
    """
    doc = {"name": "", "contact_lines": [], "sections": []}
    current = None
    in_header = True
    lines = text.strip().splitlines()

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # H1 → name
        if line.startswith("# "):
            doc["name"] = line[2:].strip()
            continue

        # H2 → new section
        if line.startswith("## "):
            in_header = False
            title = line[3:].strip()
            current = {
                "title": title,
                "style": section_style(title),
                "entries": []
            }
            doc["sections"].append(current)
            continue

        # Fallback: bare section name without "## " prefix (LLM sometimes omits ##)
        # Detect: line is ALL-CAPS word(s) OR lowercase is in KNOWN_BARE_SECTIONS
        # Must not look like contact/content: no @, no digits run, no bullet, no **
        _line_low = line.lower()
        _is_bare_section = (
            _line_low in KNOWN_BARE_SECTIONS
            or (line.isupper() and len(line) >= 3 and len(line) <= 40
                and not any(c in line for c in ("@", ":", "|", "-", "/")))
        )
        if _is_bare_section and not line.startswith(("- ", "* ", "• ", "**")):
            in_header = False
            title = line.strip()
            current = {
                "title": title,
                "style": section_style(title),
                "entries": []
            }
            doc["sections"].append(current)
            continue

        # Header area (before first ##)
        if in_header:
            doc["contact_lines"].append(line)
            continue

        if current is None:
            continue

        style = current["style"]

        # List section — each line is one full-width bullet (certifications, awards)
        if style == "list":
            text = line.lstrip("-•* ").strip()
            if text:
                current["entries"].append({"type": "bullet", "text": text})
            continue

        # Grid section — collect comma/pipe separated items
        if style == "grid":
            items = [i.strip().lstrip("•-").strip()
                     for i in re.split(r'[|,•]', line) if i.strip()]
            if items:
                current["entries"].append({"type": "grid", "items": items})
            continue

        # Skills section — "**Label:** items" or "Label: items"
        if style == "skills":
            m = re.match(r'\*?\*?([^:]+?)\*?\*?:\*?\*?\s*(.+)', line)
            if m:
                current["entries"].append({
                    "type": "skill",
                    "label": m.group(1).strip().strip("*").strip(),
                    "items": re.sub(r'^\*+\s*', '', m.group(2).strip()),
                })
            elif line.startswith("- "):
                clean, links = strip_links(line[2:])
                current["entries"].append({"type": "bullet", "text": clean, "links": links})
            continue

        # Primary entry: **...**
        if line.startswith("**") and line.endswith("**"):
            inner = line.strip("*").strip()
            clean, links = strip_links(inner)
            entry = _parse_primary_entry(clean, links)
            current["entries"].append(entry)
            continue

        # Plain bold line NOT ending with ** — e.g. "**Optum**" or "**Optum | Location**"
        # LLM sometimes splits company and role across two lines
        if line.startswith("**") and not line.endswith("**"):
            inner = line.strip("*").strip()
            clean, links = strip_links(inner)
            entry = _parse_primary_entry(clean, links)
            current["entries"].append(entry)
            continue

        # Bullet
        if line.startswith("- "):
            clean, links = strip_links(line[2:])
            current["entries"].append({"type": "bullet", "text": clean, "links": links})
            continue

        # Italic subtitle — could be role line following a company-only primary
        if line.startswith("*") and line.endswith("*") and not line.startswith("**"):
            italic_text = line.strip("*").strip()
            entries = current["entries"]
            # If previous entry is a primary with no role, attach this as the role
            if (entries and entries[-1]["type"] == "primary"
                    and not entries[-1].get("role")):
                entries[-1]["role"] = italic_text
            else:
                current["entries"].append({"type": "italic", "text": italic_text})
            continue

        # Plain text — if previous entry is a primary with no role, this might be the role line
        # e.g. LLM outputs: **Optum | Bengaluru** then "Data Scientist | Dec 2022 - Present"
        entries = current["entries"] if current else []
        if (entries and entries[-1]["type"] == "primary"
                and not entries[-1].get("role")
                and not line.startswith("-")):
            # Try to extract role and date from this line
            clean_line, _ = strip_links(line)
            sub_parts = [p.strip() for p in clean_line.split("|")]
            if len(sub_parts) >= 2 and _looks_like_date(sub_parts[-1]):
                entries[-1]["role"] = sub_parts[0]
                entries[-1]["date"] = sub_parts[-1]
                continue
            elif len(sub_parts) == 1 and _looks_like_role(clean_line):
                entries[-1]["role"] = clean_line
                continue

        # Plain text fallback
        clean, links = strip_links(line)
        current["entries"].append({"type": "text", "text": clean, "links": links})

    return doc


# ── PDF ENGINE ────────────────────────────────────────────────────────────────

class ResumePDF(FPDF):

    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=MB)
        self.set_margins(ML, MT, MR)
        self.pw = 210 - ML - MR   # printable width

        # Use Helvetica — built-in PDF font, zero embedding, maximum ATS compatibility
        # ATS systems extract text from glyph streams; embedded custom fonts often fail
        self.set_font("Helvetica", "", 11)

    # ── Utilities ─────────────────────────────────────────────────────────────

    def f(self, style="", size=10.5):
        self.set_font("Helvetica", style, size)

    def color(self, r, g, b):
        self.set_text_color(r, g, b)

    def black(self):
        self.set_text_color(0, 0, 0)

    def inline_write(self, tokens: list, size=10.5, link_color=(0, 0, 200)):
        """Write inline-styled tokens on current line using write()."""
        for style, text in tokens:
            self.f(style, size)
            self.write(LH, text)

    def inline_width(self, tokens: list, size=10.5) -> float:
        total = 0
        for style, text in tokens:
            self.f(style, size)
            total += self.get_string_width(text)
        return total

    # ── Header ────────────────────────────────────────────────────────────────

    def render_name(self, name: str):
        # Small-caps effect: render in bold, slightly larger
        self.f("B", 22)
        self.cell(0, 11, sanitize(name).upper(), ln=True, align="C")

    def render_contact(self, lines: list):
        self.f("", 9.5)
        self.color(50, 50, 50)
        for line in lines:
            self.cell(0, 5, sanitize(line), ln=True, align="C")
        self.black()
        self.ln(3)

    # ── Section title ─────────────────────────────────────────────────────────

    def render_section_title(self, title: str):
        self.ln(2)
        self.f("B", 11.5)
        self.black()
        self.cell(0, 7, sanitize(title), ln=True)
        y = self.get_y()
        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.4)
        self.line(ML, y, 210 - MR, y)
        self.ln(2.5)

    # ── Primary entry ─────────────────────────────────────────────────────────

    def render_primary(self, entry: dict):
        company = entry["company"]
        role    = entry.get("role", "")
        date    = entry.get("date", "")

        # ── Page break guard ─────────────────────────────────────────────────
        # Need space for: gap(4) + company(6.5) + role(5.5) + at least 1 bullet(LH*2)
        min_h = 4 + 6.5 + (5.5 if role else 0) + LH * 2
        if self.get_y() + min_h > self.h - self.b_margin:
            self.add_page()

        # Visual gap before each company block
        self.ln(4)

        # ── Company name: larger + bold, date right-aligned ──────────────────
        date_str = sanitize(date)
        self.set_font("Helvetica", "B", 11.5)
        date_w = self.get_string_width(date_str) + 2

        y = self.get_y()
        self.set_xy(ML, y)
        self.cell(self.pw - date_w, 6.5, sanitize(company), ln=False)

        # Date — right-aligned, muted
        self.set_font("Helvetica", "", 10)
        self.color(80, 80, 80)
        self.set_xy(ML + self.pw - date_w, y)
        self.cell(date_w, 6.5, date_str, ln=True, align="R")
        self.black()

        # ── Role: italic below company ────────────────────────────────────────
        if role:
            self.set_font("Helvetica", "I", 10)
            self.color(70, 70, 70)
            self.set_x(ML)
            self.cell(0, 5.5, sanitize(role), ln=True)
            self.black()

        self.ln(2)

    # ── Bullet ────────────────────────────────────────────────────────────────

    def render_bullet(self, entry: dict):
        """Flatten tokens to plain text and wrap with multi_cell."""
        text = entry.get("text", "")
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        text = sanitize(text)

        self.f("", 10.5)
        self.set_x(ML + BULLET_INDENT)
        self.multi_cell(self.pw - BULLET_INDENT, LH, f"- {text}", align="L")
        self.ln(0.5)

    def _render_tokens_wrapped(self, tokens: list, x_indent: float = None):
        """Flatten tokens to plain string and use multi_cell for safe wrapping."""
        if x_indent is None:
            x_indent = ML
        flat = sanitize("".join(t for _, t in tokens))
        self.f("", 10.5)
        self.set_x(x_indent)
        cell_w = self.w - self.r_margin - x_indent
        self.multi_cell(cell_w, LH, flat, align="L")

    # ── Skills rows ───────────────────────────────────────────────────────────

    def render_skill_row(self, entry: dict):
        # Strip any stray ** the LLM left in label or items
        label = entry.get("label", "").strip().strip("*").strip()
        items = entry.get("items", "").strip().strip("*").strip()
        # Also strip leading ** from items e.g. "** Python, SQL" → "Python, SQL"
        label = _re.sub(r"^[*]+\s*", "", label)

        # Page break guard
        if self.get_y() + LH * 2 > self.h - self.b_margin:
            self.add_page()

        y = self.get_y()
        self.f("B", 10.5)
        label_w = self.get_string_width(label + ": ") + 1
        self.set_xy(ML, y)
        self.cell(label_w, LH, sanitize(label) + ":", ln=False)

        self.f("", 10.5)
        self.set_xy(ML + label_w, y)
        self.multi_cell(self.pw - label_w, LH, sanitize(items), align="L")
        self.ln(0.5)

    # ── Grid (multi-column bullets) ───────────────────────────────────────────

    def render_grid(self, all_items: list):
        """Render all grid items in GRID_COLS columns."""
        col_w = self.pw / GRID_COLS
        self.f("", 10.5)

        col = 0
        row_y = self.get_y()

        for item in all_items:
            x = ML + col * col_w
            self.set_xy(x, row_y)
            self.cell(col_w, LH, f"- {sanitize(item)}", ln=False)
            col += 1
            if col >= GRID_COLS:
                col = 0
                row_y += LH + 1
                self.set_y(row_y)

        if col > 0:
            self.set_y(row_y + LH + 1)
        self.ln(1)

    # ── List (vertical bullets, full width) ─────────────────────────────────────

    def render_list_item(self, text: str):
        """Full-width bullet — used for certifications, awards etc."""
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        text = sanitize(text.lstrip("-• ").strip())
        if not text:
            return
        self.f("", 10.5)
        self.set_x(ML + BULLET_INDENT)
        self.multi_cell(self.pw - BULLET_INDENT, LH, f"- {text}", align="L")
        self.ln(0.5)

    # ── Italic line ───────────────────────────────────────────────────────────

    def render_italic(self, entry: dict):
        self.f("I", 10.5)
        self.color(60, 60, 60)
        self.cell(0, LH, sanitize(entry.get("text", "")), ln=True)
        self.black()
        self.ln(0.5)

    # ── Summary section (flowing paragraph text) ──────────────────────────────

    def render_summary_entry(self, entry: dict):
        """Render a SUMMARY section entry — paragraph text or bullet."""
        t = entry.get("type", "text")
        if t == "bullet":
            self.render_bullet(entry)
        else:
            text = entry.get("text", "")
            text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
            text = re.sub(r'\*(.+?)\*', r'\1', text)
            text = sanitize(text)
            if not text:
                return
            self.f("", 10.5)
            self.set_x(ML)
            self.multi_cell(self.pw, LH, text, align="L")
            self.ln(1)

    # ── Plain text ────────────────────────────────────────────────────────────

    def render_text(self, entry: dict):
        text = entry.get("text", "")
        # Strip stray ** markers before tokenizing
        text = re.sub(r'\*\*\s*', '', text)
        tokens = parse_inline(text)
        self._render_tokens_wrapped(tokens, x_indent=ML)
        self.ln(1.5)

    # ── Build full PDF ────────────────────────────────────────────────────────

    def build(self, doc: dict):
        self.add_page()
        # ATS metadata — helps parsers identify the document correctly
        self.set_title(doc.get("name", "Resume"))
        self.set_author(doc.get("name", ""))
        self.set_creator("Resume Builder")

        if doc["name"]:
            self.render_name(doc["name"])
        if doc["contact_lines"]:
            self.render_contact(doc["contact_lines"])

        for section in doc["sections"]:
            self.render_section_title(section["title"])
            style = section["style"]
            entries = section["entries"]

            if style == "summary":
                for e in entries:
                    self.render_summary_entry(e)

            elif style == "list":
                # Full-width vertical bullet list — certifications, awards etc.
                for e in entries:
                    raw = e.get("text", "") or e.get("items", [""])[0]
                    self.render_list_item(raw)

            elif style == "grid":
                all_items = []
                for e in entries:
                    all_items.extend(e.get("items", []))
                self.render_grid(all_items)

            elif style == "skills":
                for e in entries:
                    t = e["type"]
                    if t == "skill":
                        self.render_skill_row(e)
                    elif t == "bullet":
                        self.render_bullet(e)

            else:  # standard
                prev_type = None
                for idx, e in enumerate(entries):
                    t = e["type"]

                    if t == "primary":
                        # Look ahead: find how many bullets follow this primary
                        # Guard: if primary + role + first bullet won't fit, push to next page
                        next_entries = entries[idx+1:idx+3]
                        has_bullets  = any(n["type"] == "bullet" for n in next_entries)
                        extra_h      = LH * 2.5 if has_bullets else 0
                        needed       = 4 + 6.5 + 5.5 + extra_h  # gap+company+role+bullets
                        if self.get_y() + needed > self.h - self.b_margin:
                            self.add_page()
                        # Extra gap after last bullet before new company
                        if prev_type == "bullet":
                            self.ln(2)
                        self.render_primary(e)

                    elif t == "bullet":
                        self.render_bullet(e)
                    elif t == "italic":
                        self.render_italic(e)
                    elif t == "text":
                        self.render_text(e)
                    elif t == "skill":
                        self.render_skill_row(e)

                    prev_type = t


# ── PUBLIC API ────────────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    """Remove ```markdown / ``` code fences the LLM sometimes wraps around content."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first line (``` or ```markdown)
        lines = lines[1:]
        # Drop last line if it's just ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def generate_resume_pdf(llm_text: str, output_path: str) -> str:
    """
    Main function. Call this from your pipeline.
    llm_text    : raw markdown string from LLM
    output_path : e.g. "output/resume.pdf"
    Returns     : output_path on success

    Pipeline:
      1. Strip code fences      — LLM sometimes wraps content in ```markdown...```
      2. validate_resume_md()   — fix markdown before parsing
      3. parse_resume()         — parse to structured dict
      4. validate_parsed_doc()  — fix structure after parsing
      5. ResumePDF.build()      — render PDF
    """
    # Step 0: Strip any code fences
    llm_text = _strip_fences(llm_text)
    if not llm_text:
        raise ValueError("Empty resume text — nothing to render.")

    # Step 1: Fix markdown
    fixed_md, md_fixes = validate_resume_md(llm_text)
    if md_fixes:
        print(f"  Markdown fixes applied ({len(md_fixes)}):")
        for fix in md_fixes[:6]:
            print(f"    - {fix}")

    # Step 2: Parse
    doc = parse_resume(fixed_md)

    # Step 3: Fix structure
    doc, struct_warnings = validate_parsed_doc(doc)
    if struct_warnings:
        print(f"  Structure warnings ({len(struct_warnings)}):")
        for w in struct_warnings[:6]:
            print(f"    - {w}")

    # Step 4: Render
    pdf = ResumePDF()
    pdf.build(doc)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    pdf.output(output_path)
    print(f"[OK] Resume saved -> {output_path}")
    return output_path


# ── DEMO ─────────────────────────────────────────────────────────────────────

SAMPLE = """
# Pradeep M
Hyderabad, Telangana
+91-99999 99999 | pradeepm.analyst@gmail.com | linkedin.com/in/pradeepanalyst | Portfolio

## Profile Summary
- Proven ability in analyzing large datasets, debugging SQL queries, and transforming data to drive business decisions.
- Proficient in creating compelling, interactive dashboards using Power BI, enhancing data accessibility.
- Strong command over Excel, SQL, Power BI, enabling efficient data manipulation and analysis.

## Relevant Coursework
Data Integrity | Data Governance | Generative AI | Requirement Gathering | Data Visualization | Data Manipulation | Data Mining | Business Impact Analysis

## Experience
**Deloitte - Data Integrity & Reporting Analyst | June 2024 – Present**
- Manage and enhance client data across multiple CRM tools, ensuring accurate and up-to-date information.
- Perform lead verification by researching on LinkedIn and other sources to identify and correct data inconsistencies.
- Oversee data accuracy and consistency within Deloitte's databases through ongoing validation, audits, and updates.

**AtliQ Technologies - Data Analyst Intern | Mar 2024 – Mar 2024**
- Performed **data variance analysis** and debugged SQL queries, demonstrating **critical thinking** and **attention to detail** to ensure 100% accuracy in data extraction from MySQL Workbench.
- Applied advanced data cleaning and **data normalization techniques**, showcasing **problem-solving skills** to maintain data integrity and optimize retrieval processes.

## Projects
**Festive Campaign Analysis | SQL, Power BI, PowerPoint | Feb 2024**
- During Diwali and Sankranti campaigns across 50+ Southern Indian retail stores, utilized SQL to analyze transactional data and identify purchase trends. Built an **interactive Power BI dashboard** for enhanced data visualization.

**Business Insights 360 | Excel, Power BI, DAX, Power Query, SQL | Jan 2024**
- Modernized AtliQ's reporting by replacing Excel with a Power BI dashboard integrating data from Excel/CSV and SQL. Created a data model and visualizations across 5 departments, optimizing with DAX Studio for a **5% performance boost**.

## Patents
**Automated Data Anomaly Detection System | Filed Jan 2024**
- Filed a patent for a novel algorithm detecting data anomalies in CRM systems using statistical variance analysis.
- Reduces false positive rate by 62% compared to existing rule-based approaches.

## Technical Skills
**Analytical Tools:** Excel, Power BI, Power Query, Tableau
**Languages:** Python, SQL
**Technologies/Frameworks:** GitHub, WordPress, Pandas, NumPy

## Education
**Vellore Institute of Technology | Bachelor of Science – CGPA: 8.45 | Aug 2020 – May 2023 | Vellore, Tamil Nadu**

## Extracurricular / Certifications
- Accenture North America: Data Analytics And Visualization
- Tata Data Visualization: Empowering Business With Effective Insights
- Google Data Analytics, Coursera
- Codebasics Community Champion
"""

if __name__ == "__main__":
    generate_resume_pdf(SAMPLE, "output/pradeep_resume.pdf")