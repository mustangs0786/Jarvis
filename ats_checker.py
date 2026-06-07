"""
ats_checker.py — ATS Score Checker
====================================
Tries jobswagon.com first (Selenium), falls back to our own checker.

Install: uv pip install selenium webdriver-manager pdfplumber
"""

import re
import time
import pdfplumber
from pathlib import Path
from dataclasses import dataclass, field

# ── Result data model ─────────────────────────────────────────────────────────

@dataclass
class ATSResult:
    source: str          # "jobswagon", "Gemini", "error"
    overall_score: int   # 0-100
    parse_rate: int      # 0-100
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    sections_found: list[str] = field(default_factory=list)
    sections_missing: list[str] = field(default_factory=list)
    keyword_hits: list[str] = field(default_factory=list)
    keyword_misses: list[str] = field(default_factory=list)
    grade: str = ""       # A / B / C / D
    summary: str = ""     # plain English summary
    raw_text: str = ""

    def classify_issues(self) -> tuple[list[str], list[str]]:
        """
        Split issues into two buckets:
        - formatting_issues : fixable by rerunning agent (verbs, dates, structure)
        - skill_gaps        : require user to actually have the skill

        Returns (formatting_issues, skill_gaps)
        """
        FORMATTING_KEYWORDS = [
            "verb", "action", "bullet", "date", "format", "section", "header",
            "spacing", "font", "asterisk", "character", "length", "quantif",
            "number", "metric", "percent", "impact", "weak", "passive",
            "tense", "abbreviat", "acronym", "contact", "phone", "email",
            "linkedin", "summary", "objective", "parse", "encoding", "tab",
            "column", "table", "graphic", "image", "inconsist", "whitespace",
        ]
        SKILL_KEYWORDS = [
            "missing", "not found", "absent", "lack", "no experience",
            "required skill", "keyword", "technology", "tool", "framework",
            "language", "certification", "degree", "years of experience",
        ]

        formatting = []
        skill_gaps = []

        all_issues = self.issues + [f"Missing keyword: {k}" for k in self.keyword_misses]

        for issue in all_issues:
            lower = issue.lower()
            is_format = any(kw in lower for kw in FORMATTING_KEYWORDS)
            is_skill  = any(kw in lower for kw in SKILL_KEYWORDS)

            if is_format and not is_skill:
                formatting.append(issue)
            elif is_skill:
                skill_gaps.append(issue)
            else:
                # Default to formatting — better to try fixing than to surface as skill gap
                formatting.append(issue)

        return formatting, skill_gaps

    def has_fixable_issues(self) -> bool:
        formatting, _ = self.classify_issues()
        return len(formatting) > 0 and self.overall_score < 90

    def format_for_telegram(self) -> str:
        score_emoji = "✅" if self.overall_score >= 80 else "⚠️" if self.overall_score >= 60 else "❌"
        grade_str = f"  Grade: *{self.grade}*" if self.grade else ""
        lines = [
            f"📊 *ATS Analysis* _via {self.source}_",
            "",
            f"{score_emoji} *Overall Score: {self.overall_score}/100*{grade_str}",
            f"📄 Parse Rate: {self.parse_rate}%",
        ]

        if self.summary:
            lines += ["", f"_{self.summary}_"]

        if self.strengths:
            lines += ["", "✅ *Strengths*"]
            lines += [f"  - {s}" for s in self.strengths[:3]]

        if self.sections_missing:
            lines += ["", "❌ *Missing Sections*"]
            lines += [f"  - {s}" for s in self.sections_missing[:4]]

        if self.issues:
            lines += ["", "⚠️ *Issues Found*"]
            lines += [f"  - {i}" for i in self.issues[:5]]

        if self.keyword_misses:
            lines += ["", "🎯 *ATS Keywords to Add*"]
            lines += [f"  - {k}" for k in self.keyword_misses[:6]]

        if self.suggestions:
            lines += ["", "💡 *Quick Fixes*"]
            lines += [f"  - {s}" for s in self.suggestions[:4]]

        return "\n".join(lines)


# ── OPTION A: Jobswagon Selenium ──────────────────────────────────────────────

def check_with_jobswagon(pdf_path: str, timeout: int = 60) -> ATSResult | None:
    """
    Upload PDF to jobswagon.com and parse the result page.
    Returns ATSResult on success, None on failure.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service as ChromeService
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError:
        print("  Selenium not available — skipping jobswagon")
        return None

    print("  Trying jobswagon.com...")

    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    driver = None
    try:
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

        # ── Step 1: Load homepage ──
        driver.get("https://jobswagon.com/")
        wait = WebDriverWait(driver, 20)

        # ── Step 2: Find file input and upload ──
        # Try common input selectors
        file_input = None
        for selector in [
            "input[type='file']",
            "input[accept*='pdf']",
            "input[accept*='.pdf']",
            "#resume-upload",
            ".upload-input",
        ]:
            try:
                file_input = driver.find_element(By.CSS_SELECTOR, selector)
                if file_input:
                    break
            except Exception:
                continue

        if not file_input:
            print("  Could not find file input on jobswagon")
            return None

        # Send the file path directly to the input
        abs_path = str(Path(pdf_path).resolve())
        file_input.send_keys(abs_path)
        print("  File uploaded, waiting for analysis...")

        # ── Step 3: Wait for results to load ──
        # Wait for score element to appear
        time.sleep(3)
        result_loaded = False

        for result_selector in [
            "[class*='score']",
            "[class*='result']",
            "[class*='analysis']",
            "[class*='report']",
            "[id*='score']",
            "[id*='result']",
        ]:
            try:
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, result_selector)))
                result_loaded = True
                print(f"  Results loaded ({result_selector})")
                break
            except Exception:
                continue

        if not result_loaded:
            # Give it extra time — jobswagon says "under 30 seconds"
            time.sleep(20)

        # ── Step 4: Parse the results page ──
        page_text = driver.find_element(By.TAG_NAME, "body").get_attribute("innerText")
        return _parse_jobswagon_results(page_text)

    except Exception as e:
        print(f"  Jobswagon error: {e}")
        return None

    finally:
        if driver:
            driver.quit()


def _parse_jobswagon_results(page_text: str) -> ATSResult | None:
    """Parse jobswagon result page text into ATSResult."""
    if not page_text or len(page_text) < 100:
        return None

    text_lower = page_text.lower()

    # Extract score — look for patterns like "72/100", "Score: 72", "72%"
    score = 0
    for pattern in [
        r'(\d{1,3})\s*/\s*100',
        r'score[:\s]+(\d{1,3})',
        r'(\d{1,3})\s*%',
        r'(\d{1,3})\s*out of\s*100',
    ]:
        m = re.search(pattern, text_lower)
        if m:
            val = int(m.group(1))
            if 0 <= val <= 100:
                score = val
                break

    # Extract issues — lines containing negative words
    issues = []
    for line in page_text.split('\n'):
        line = line.strip()
        if not line or len(line) < 10:
            continue
        lower = line.lower()
        if any(w in lower for w in ['missing', 'not found', 'no ', 'lacks', 'absent',
                                     'improve', 'add ', 'should ', 'consider', 'weak']):
            if len(line) < 200:  # skip huge paragraphs
                issues.append(line)

    # Extract suggestions — lines with action words
    suggestions = []
    for line in page_text.split('\n'):
        line = line.strip()
        if not line or len(line) < 10:
            continue
        lower = line.lower()
        if any(w in lower for w in ['add', 'include', 'use', 'try', 'consider',
                                     'recommend', 'suggest']):
            if len(line) < 200:
                suggestions.append(line)

    # Parse rate — look for parse-related percentages
    parse_rate = score  # default to overall score if not found separately
    for pattern in [r'parse[d\s]+(\d{1,3})\s*%', r'(\d{1,3})\s*%.*?pars']:
        m = re.search(pattern, text_lower)
        if m:
            parse_rate = int(m.group(1))
            break

    if score == 0 and not issues:
        print("  Could not parse meaningful results from jobswagon")
        return None

    return ATSResult(
        source="jobswagon.com",
        overall_score=score,
        parse_rate=parse_rate,
        issues=issues[:6],
        suggestions=suggestions[:4],
    )


# ── OPTION B: Gemini-based ATS checker ──────────────────────────────────────

def _extract_pdf_text(pdf_path: str) -> tuple[str, int]:
    """
    Extract text from PDF using pdfplumber.
    Returns (text, parse_rate_0_to_100)
    """
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        text = "\n".join(pages).strip()
        parse_rate = 95 if len(text) > 500 else 50 if len(text) > 100 else 20
        return text, parse_rate
    except ImportError:
        # pdfplumber not installed — read raw bytes and pass to Gemini as file
        return "", 0
    except Exception as e:
        print(f"  PDF extraction error: {e}")
        return "", 0


def _build_ats_prompt(resume_text: str, job_description: str, parse_rate: int) -> str:
    jd_section = f"""
## JOB DESCRIPTION (use for keyword matching):
{job_description[:3000]}
""" if job_description else "No job description provided — do general ATS analysis only."

    return f"""
You are a senior ATS (Applicant Tracking System) specialist with deep knowledge of how
enterprise ATS systems like Workday, Taleo, Greenhouse, and Lever parse and score resumes.

Analyze this resume text extracted from a PDF and give a detailed ATS audit.

## RESUME TEXT (extracted from PDF, parse rate: {parse_rate}%):
{resume_text[:4000]}

{jd_section}

## YOUR ANALYSIS TASK:

Evaluate the resume as an ATS system would. Be specific and actionable.

Check for:
1. Parse rate issues — special characters, encoding problems, non-standard fonts
2. Section detection — are all standard sections present and clearly labeled?
3. Contact info — email, phone, LinkedIn all parseable?
4. Keyword coverage — how many JD keywords appear naturally in the resume?
5. Bullet quality — STAR format, action verbs, quantified achievements?
6. Formatting issues — tables, columns, graphics that confuse ATS?
7. Date consistency — are all dates in a consistent format?
8. File/content issues — any garbled text, asterisks, or encoding artifacts?

## OUTPUT FORMAT:
Return a single valid JSON object only, no markdown fences.

{{
  "overall_score": <integer 0-100>,
  "parse_rate": <integer 0-100, use {parse_rate} as base, adjust based on text quality>,
  "grade": "<A / B / C / D>",
  "summary": "<2 sentence plain-English summary of the resume's ATS fitness>",
  "sections_found": ["<section name>", ...],
  "sections_missing": ["<section name>", ...],
  "keyword_hits": ["<keyword found in resume>", ...],
  "keyword_misses": ["<important JD keyword NOT in resume>", ...],
  "issues": [
    "<specific issue #1 — be concrete, e.g. 'Asterisks found in skills section'>",
    "<specific issue #2>",
    "<specific issue #3>"
  ],
  "suggestions": [
    "<actionable fix #1 — e.g. 'Add LangChain to Technical Skills section'>",
    "<actionable fix #2>",
    "<actionable fix #3>"
  ],
  "strengths": [
    "<something done well — e.g. 'Strong quantification: 8 metrics found'>",
    "<another strength>"
  ]
}}
"""


def check_with_gemini(pdf_path: str, job_description: str = "") -> ATSResult:
    """
    Gemini-based ATS checker.
    Extracts text from PDF with pdfplumber, then sends to Gemini for deep analysis.
    """
    print("  Running Gemini ATS analysis...")

    # Step 1: Extract text from PDF
    resume_text, parse_rate = _extract_pdf_text(pdf_path)

    if not resume_text:
        # pdfplumber failed — upload PDF directly to Gemini File API
        print("  Text extraction failed — uploading PDF to Gemini directly...")
        try:
            import time as _time
            from google import genai
            from google.genai import types
            from dotenv import load_dotenv
            import os
            load_dotenv()
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

            uploaded = client.files.upload(
                file=pdf_path,
                config=types.UploadFileConfig(mime_type="application/pdf")
            )
            # Wait for file to be ready
            for _ in range(15):
                info = client.files.get(name=uploaded.name)
                if "ACTIVE" in str(info.state).upper():
                    break
                _time.sleep(1)

            prompt = _build_ats_prompt("[See attached PDF]", job_description, 0)
            response = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=[
                    types.Part.from_uri(file_uri=uploaded.uri, mime_type="application/pdf"),
                    prompt
                ],
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json"
                )
            )
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass

            return _parse_gemini_response(response.text, source="Gemini (PDF)")

        except Exception as e:
            print(f"  Gemini PDF upload failed: {e}")
            return _fallback_error_result(str(e))

    # Step 2: Send extracted text to Gemini
    try:
        from google import genai
        from google.genai import types
        from dotenv import load_dotenv
        import os, json
        load_dotenv()
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

        prompt = _build_ats_prompt(resume_text, job_description, parse_rate)
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json"
            )
        )
        return _parse_gemini_response(response.text, source="Gemini")

    except Exception as e:
        print(f"  Gemini ATS analysis failed: {e}")
        return _fallback_error_result(str(e))


def _parse_gemini_response(raw: str, source: str = "Gemini") -> ATSResult:
    """Parse Gemini JSON response into ATSResult."""
    import json
    raw = raw.strip().replace("```json", "").replace("```", "").strip()
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return _fallback_error_result("Invalid JSON from Gemini")

    return ATSResult(
        source=source,
        overall_score=int(d.get("overall_score", 0)),
        parse_rate=int(d.get("parse_rate", 0)),
        issues=d.get("issues", []),
        suggestions=d.get("suggestions", []),
        sections_found=d.get("sections_found", []),
        sections_missing=d.get("sections_missing", []),
        keyword_hits=d.get("keyword_hits", []),
        keyword_misses=d.get("keyword_misses", []),
        strengths=d.get("strengths", []),
        grade=d.get("grade", ""),
        summary=d.get("summary", ""),
    )


def _fallback_error_result(error: str) -> ATSResult:
    return ATSResult(
        source="error",
        overall_score=0,
        parse_rate=0,
        issues=[f"ATS check failed: {error}"],
    )


# ── PUBLIC API ────────────────────────────────────────────────────────────────

def check_ats_score(pdf_path: str, job_description: str = "") -> ATSResult:
    """
    Main entry point.
    1. Try jobswagon.com (Selenium)
    2. Fallback to Gemini-based analysis
    """
    # Try jobswagon
    result = check_with_jobswagon(pdf_path)
    if result and result.overall_score > 0:
        print(f"  Jobswagon score: {result.overall_score}/100")
        # Augment with Gemini keyword analysis if JD provided
        if job_description:
            try:
                gemini = check_with_gemini(pdf_path, job_description)
                result.keyword_hits   = gemini.keyword_hits
                result.keyword_misses = gemini.keyword_misses
                result.sections_found = gemini.sections_found
                result.suggestions    = gemini.suggestions or result.suggestions
            except Exception:
                pass
        return result

    # Fallback to Gemini
    print("  Jobswagon unavailable — using Gemini ATS checker")
    return check_with_gemini(pdf_path, job_description)


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    pdf = sys.argv[1] if len(sys.argv) > 1 else "output/resume.pdf"
    jd  = sys.argv[2] if len(sys.argv) > 2 else ""

    if not Path(pdf).exists():
        print(f"File not found: {pdf}")
        sys.exit(1)

    print(f"\nChecking ATS score for: {pdf}\n")
    result = check_ats_score(pdf, jd)

    print(f"\nSource     : {result.source}")
    print(f"Score      : {result.overall_score}/100  Grade: {result.grade}")
    print(f"Parse Rate : {result.parse_rate}%")
    if result.summary:
        print(f"Summary    : {result.summary}")
    if result.issues:
        print(f"\nIssues:")
        for i in result.issues: print(f"  - {i}")
    if result.suggestions:
        print(f"\nSuggestions:")
        for s in result.suggestions: print(f"  - {s}")
    if result.keyword_misses:
        print(f"\nMissing keywords:")
        for k in result.keyword_misses: print(f"  - {k}")