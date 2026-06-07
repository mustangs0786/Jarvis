"""
job_fetcher.py — LinkedIn job feed (India-focused)
===================================================
Source: LinkedIn guest API (no login required)

Features:
  - Synonym-aware search (AI/ML engineer → data scientist, applied scientist, etc.)
  - Post-fetch title relevance filter (blocks SDE, hardware, janitorial noise)
  - Location filter (Indian cities — verified geoIds from LinkedIn typeahead API)
  - Experience level filter (LinkedIn f_E parameter)
  - Deduplication + sorted newest-first

Install: uv pip install requests beautifulsoup4
"""

import re
import time
import requests
from dataclasses import dataclass, field
from typing import Optional
from bs4 import BeautifulSoup

# ── Title relevance filter ─────────────────────────────────────────────────────
# Applied AFTER fetching to reject irrelevant jobs (LinkedIn returns noise).

# Phrases that unambiguously signal an AI/ML/data role
_AI_ML_PHRASES = [
    "machine learning", "deep learning", "data scien", "data engineer",
    "data analyst", "ml engineer", "ai engineer", "nlp engineer",
    "llm engineer", "genai engineer", "generative ai", "computer vision",
    "applied scientist", "research scientist", "mlops", "analytics engineer",
    "artificial intelligence", "neural network", "python developer",
    "reinforcement learning", "large language model", "recommendation",
    "ai researcher", "ml researcher", "ai architect", "ml architect",
    "conversational ai", "speech recognition", "knowledge engineer",
]

# Short/ambiguous domain words — matched as whole words via regex \b
_DOMAIN_RE = re.compile(
    r'\b(ai|ml|nlp|llm|llms|gpt|llm|rag|genai|data|analytics|intelligence|'
    r'generative|neural|mlops|python|science|scientist|machine|vision|'
    r'prediction|forecasting|statistical|quantitative)\b',
    re.IGNORECASE,
)

# Role words that, combined with a domain word, make it relevant
_ROLE_RE = re.compile(
    r'\b(engineer|developer|scientist|analyst|researcher|architect|specialist|'
    r'lead|manager|consultant)\b',
    re.IGNORECASE,
)


def _is_relevant_title(title: str) -> bool:
    """True if the job title is actually an AI/ML/data role (not SDE, hardware, janitor…)."""
    t = title.lower()
    # 1. Exact phrase match — highest confidence
    if any(phrase in t for phrase in _AI_ML_PHRASES):
        return True
    # 2. Must have BOTH a domain signal AND a role signal
    return bool(_DOMAIN_RE.search(title) and _ROLE_RE.search(title))

# ── Role synonym expansion ─────────────────────────────────────────────────────
# When user searches for a role, we also search for these synonyms in parallel.
# Keys are normalised lowercase. Values are the additional LinkedIn keywords.

ROLE_SYNONYMS: dict[str, list[str]] = {
    "ai engineer":          ["ai engineer genai", "artificial intelligence engineer", "llm engineer"],
    "ai/ml engineer":       ["ai engineer", "ml engineer", "machine learning engineer",
                             "applied scientist", "data scientist", "GenAI engineer"],
    "ml engineer":          ["machine learning engineer", "mlops engineer", "ai engineer"],
    "machine learning":     ["machine learning engineer", "ml engineer", "ai engineer"],
    "data scientist":       ["data scientist", "applied scientist", "research scientist ml",
                             "quantitative researcher"],
    "applied scientist":    ["applied scientist", "research scientist ml", "data scientist"],
    "research scientist":   ["research scientist ml", "applied scientist", "data scientist"],
    "data engineer":        ["data engineer", "big data engineer", "analytics engineer", "etl developer"],
    "nlp engineer":         ["nlp engineer", "natural language processing engineer", "text mining"],
    "computer vision":      ["computer vision engineer", "cv engineer", "image recognition engineer"],
    "mlops":                ["mlops engineer", "ml platform engineer", "ml infrastructure engineer"],
    "analytics engineer":   ["analytics engineer", "data analyst", "business intelligence engineer"],
    "generative ai":        ["generative ai engineer", "llm engineer", "ai engineer genai"],
    "llm engineer":         ["llm engineer", "genai engineer", "ai engineer", "prompt engineer"],
}

def expand_search_terms(query: str) -> list[str]:
    """
    Return a list of search terms covering the query and all its synonyms.
    Original query is always first. Deduped.
    """
    q = query.strip().lower()
    seen = {q}
    terms = [query]
    for key, synonyms in ROLE_SYNONYMS.items():
        # Match if the key appears in the query OR query appears in the key
        if key in q or q in key or any(s.lower() in q or q in s.lower() for s in synonyms):
            for s in synonyms:
                if s.lower() not in seen:
                    seen.add(s.lower())
                    terms.append(s)
    # Cap at 4 variants to avoid too many API calls
    return terms[:4]


# ── Location map ──────────────────────────────────────────────────────────────
# LinkedIn geoId + optional work-type (f_WT) for remote

LOCATIONS: dict[str, dict] = {
    "Bengaluru":   {"geo": "105214831", "label": "Bengaluru, India"},
    "Hyderabad":   {"geo": "105556991", "label": "Hyderabad, India"},
    "Mumbai":      {"geo": "102714425", "label": "Mumbai, India"},
    "Pune":        {"geo": "115785398", "label": "Pune, India"},
    "Delhi NCR":   {"geo": "102257491", "label": "Delhi NCR, India"},
    "Chennai":     {"geo": "106563096", "label": "Chennai, India"},
    "India":       {"geo": "102713980", "label": "India"},
    "Singapore":   {"geo": "102454443", "label": "Singapore"},
    "USA":         {"geo": "103644278", "label": "United States"},
    "UK":          {"geo": "101165590", "label": "United Kingdom"},
    "Remote":      {"geo": "102713980", "work_type": "2", "label": "Remote (India)"},
    "Remote Global": {"geo": None,       "work_type": "2", "label": "Remote (Global)"},
}

# ── Experience level map ───────────────────────────────────────────────────────
# LinkedIn f_E values
EXP_LEVELS: dict[str, str] = {
    "Any":              "",
    "Fresher (0-2 yr)": "2",      # Entry Level
    "Junior (2-5 yr)":  "3",      # Associate
    "Senior (5+ yr)":   "4",      # Mid-Senior Level
    "Lead / Manager":   "5",      # Director
}

# ── Time filter presets ───────────────────────────────────────────────────────
TIME_FILTERS = {
    "1 hour":   "r3600",
    "24 hours": "r86400",
    "3 days":   "r259200",
    "7 days":   "r604800",
    "30 days":  "r2592000",
}

# ── FAANG + Big Tech company IDs ──────────────────────────────────────────────
BIG_TECH_COMPANIES = {
    "Google": "1441", "Meta": "10667", "Amazon": "1586", "Apple": "162479",
    "Netflix": "165158", "Microsoft": "1035", "Nvidia": "1560",
    "Salesforce": "3779", "Adobe": "1709", "Intel": "1053", "IBM": "1009",
    "Oracle": "1066", "SAP": "1373", "Qualcomm": "3144", "Uber": "19271500",
    "Airbnb": "391850", "LinkedIn": "1337", "Flipkart": "2748527",
    "Swiggy": "6905435", "Zomato": "1353539", "PhonePe": "15142873",
    "Razorpay": "11458159", "CRED": "18647291", "Goldman Sachs": "1067",
    "JPMorgan": "1068", "Visa": "1828", "Mastercard": "2068",
    "Accenture": "1033", "Deloitte": "1038", "Optum": "357412",
    "Databricks": "3344252", "Snowflake": "3812658", "Stripe": "3651435",
    "Atlassian": "10455", "Thoughtworks": "4836",
}
COMPANY_GROUPS = {
    "FAANG":         ["Google", "Meta", "Amazon", "Apple", "Netflix", "Microsoft"],
    "Big Tech":      ["Nvidia", "Salesforce", "Adobe", "Uber", "Databricks", "Snowflake", "Atlassian"],
    "India Unicorns":["Flipkart", "Swiggy", "Zomato", "PhonePe", "Razorpay", "CRED"],
    "MNC Finance":   ["Goldman Sachs", "JPMorgan", "Visa", "Mastercard", "Optum"],
    "All Big Tech":  list(BIG_TECH_COMPANIES.keys()),
}

# ── Job data model ────────────────────────────────────────────────────────────
@dataclass
class Job:
    title:    str
    company:  str
    location: str
    url:      str
    job_id:   str = ""
    posted:   str = ""
    source:   str = "linkedin"
    tags:     list = field(default_factory=list)

    def apply_url(self) -> str:
        return self.url.split("?")[0] if self.url else ""


# ── LinkedIn guest API ─────────────────────────────────────────────────────────
GUEST_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.linkedin.com/jobs/search/",
}


def _build_linkedin_url(
    keywords: str,
    time_filter: str = "r86400",
    start: int = 0,
    geo_id: str = "102713980",
    work_type: str = "",
    exp_level: str = "",
    company_ids: list = None,
) -> str:
    kw = keywords.strip().replace(" ", "%20")
    url = (
        f"{GUEST_API}"
        f"?keywords={kw}"
        f"&f_TPR={time_filter}"
        f"&sortBy=DD"
        f"&start={start}"
        f"&count=25"
    )
    if geo_id:
        url += f"&geoId={geo_id}"
    if work_type:
        url += f"&f_WT={work_type}"
    if exp_level:
        url += f"&f_E={exp_level}"
    if company_ids:
        url += f"&f_C={','.join(company_ids)}"
    return url


def _parse_linkedin_cards(html: str) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for card in soup.find_all("li"):
        try:
            title_el = card.find("h3", class_=re.compile("base-search-card__title|job-search-card__title"))
            if not title_el:
                title_el = card.find("span", class_=re.compile("title|job-title"))
            title = title_el.get_text(strip=True) if title_el else ""

            company_el = card.find("h4", class_=re.compile("base-search-card__subtitle|company"))
            if not company_el:
                company_el = card.find("a", class_=re.compile("company"))
            company = company_el.get_text(strip=True) if company_el else ""

            location_el = card.find("span", class_=re.compile("location|job-search-card__location"))
            location = location_el.get_text(strip=True) if location_el else ""

            link_el = card.find("a", class_=re.compile("base-card__full-link|job-card"))
            if not link_el:
                link_el = card.find("a", href=re.compile("/jobs/view/"))
            url = link_el["href"].split("?")[0] if link_el and link_el.get("href") else ""
            job_id = url.split("-")[-1] if url else ""

            time_el = card.find("time")
            posted = time_el.get("datetime", "") if time_el else ""

            if title and (company or url):
                jobs.append(Job(
                    title=title, company=company, location=location,
                    url=url, job_id=job_id, posted=posted, source="linkedin",
                ))
        except Exception:
            continue
    return jobs


def fetch_linkedin(
    keywords: str,
    time_filter: str = "r86400",
    max_jobs: int = 15,
    location: str = "Bengaluru",
    exp_level: str = "",
    big_tech_only: bool = False,
) -> list[Job]:
    """Fetch jobs from LinkedIn guest API for a single keyword set."""
    loc = LOCATIONS.get(location, LOCATIONS["Bengaluru"])
    geo_id = loc.get("geo", "102713980") or "102713980"
    work_type = loc.get("work_type", "")

    company_ids = None
    if big_tech_only:
        company_ids = list(BIG_TECH_COMPANIES.values())[:30]  # LinkedIn caps at ~30

    all_jobs: list[Job] = []
    seen_ids: set = set()

    for start in range(0, max(max_jobs, 25), 25):
        url = _build_linkedin_url(
            keywords, time_filter, start, geo_id, work_type, exp_level, company_ids
        )
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 429:
                time.sleep(20)
                resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                break

            jobs = _parse_linkedin_cards(resp.text)
            if not jobs:
                break

            for job in jobs:
                key = job.job_id or job.url
                if key and key not in seen_ids:
                    # Post-fetch title filter: LinkedIn returns noise (SDE, hardware, etc.)
                    if not _is_relevant_title(job.title):
                        continue
                    seen_ids.add(key)
                    all_jobs.append(job)
                if len(all_jobs) >= max_jobs:
                    break
        except Exception as e:
            print(f"  [LinkedIn] error: {e}")
            break

        if len(all_jobs) >= max_jobs:
            break
        time.sleep(0.8)

    return all_jobs


# ── Combined fetch (LinkedIn only, synonym-expanded) ─────────────────────────

def fetch_jobs(
    keywords: str,
    time_filter: str = "r86400",
    max_jobs: int = 25,
    locations: list = None,        # multi-location: ["Bengaluru", "Hyderabad"]
    location: str = "Bengaluru",   # legacy single-location param
    exp_level: str = "",           # LinkedIn f_E — supports "3,4" for multi-exp
    big_tech_only: bool = False,
    include_remote: bool = True,   # kept for API compat, unused
) -> list[Job]:
    """
    Fetch jobs from LinkedIn with synonym expansion + multi-location support.
    - Multiple locations → one fetch_linkedin call per location per term
    - Single location   → 2 pages per term for extra depth
    - exp_level         → passed straight to LinkedIn f_E (supports "3,4")
    Returns deduplicated list sorted newest-first, capped at max_jobs.
    """
    loc_list = locations if locations else [location]
    if not loc_list:
        loc_list = ["Bengaluru"]

    terms = expand_search_terms(keywords)
    print(f"  terms={terms} | locations={loc_list} | exp={exp_level!r}")

    all_jobs: list[Job] = []
    seen_keys: set = set()

    # With 1 location: fetch 2 pages per term (≥26 triggers page 2 in fetch_linkedin)
    # With multiple locations: 1 page per term to keep total API calls reasonable
    per_term = 26 if len(loc_list) == 1 else 25

    for loc in loc_list:
        for term in terms:
            jobs = fetch_linkedin(
                term, time_filter,
                max_jobs=per_term,
                location=loc,
                exp_level=exp_level,
                big_tech_only=big_tech_only,
            )
            for j in jobs:
                key = j.job_id or j.url
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    all_jobs.append(j)
            time.sleep(0.6)

    all_jobs.sort(key=lambda j: j.posted or "0000", reverse=True)
    return all_jobs[:max_jobs]


# ── Legacy wrapper (for bot.py compatibility) ─────────────────────────────────
def fetch_latest_jobs(searches=None, time_filter="r86400", max_per_search=10):
    from job_fetcher import DEFAULT_SEARCHES as _DS
    searches = searches or _DS
    seen, result = set(), []
    for s in searches:
        for j in fetch_linkedin(s["keywords"], time_filter, max_per_search):
            k = j.job_id or j.url
            if k not in seen:
                seen.add(k)
                result.append(j)
        time.sleep(1)
    return result


DEFAULT_SEARCHES = [
    {"label": "Data Scientist",  "keywords": "data scientist"},
    {"label": "ML Engineer",     "keywords": "machine learning engineer"},
    {"label": "AI Engineer",     "keywords": "AI engineer GenAI"},
    {"label": "Data Analyst",    "keywords": "data analyst"},
]


def format_jobs_message(jobs: list, title: str = "Latest Jobs") -> str:
    if not jobs:
        return "No jobs found.\n\nTry a wider time range or different role."
    lines = [f"*{title}*\n_{len(jobs)} jobs_\n"]
    for i, job in enumerate(jobs[:12], 1):
        url = job.url.split("?")[0] if job.url else ""
        lines.append(f"{i}\\. *{job.title}*\n   {job.company}\n   [{url}]({url})\n")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    query    = sys.argv[1] if len(sys.argv) > 1 else "AI/ML engineer"
    location = sys.argv[2] if len(sys.argv) > 2 else "Bengaluru"
    period   = sys.argv[3] if len(sys.argv) > 3 else "r86400"

    print(f"\nFetching '{query}' | {location} | {period}\n")
    jobs = fetch_jobs(query, time_filter=period, location=location, max_jobs=15)

    if not jobs:
        print("No jobs found.")
    else:
        for j in jobs:
            src = f"[{j.source}]"
            print(f"  {src} {j.title} @ {j.company} | {j.location} | {j.posted}")
            print(f"       {j.url}\n")
    print(f"Total: {len(jobs)}")
