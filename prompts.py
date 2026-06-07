"""
prompts.py — All LLM prompts for the Resume Optimization Agent
==============================================================

FLOW:
  Step 1: ANALYSIS PROMPT              → score + gap analysis (always runs)
  Step 2: Decision gate                → if score < REWRITE_THRESHOLD, return suggestions only
  Step 3: REWRITE WITH CONTEXT PROMPT  → full tailored resume (only if score >= threshold)
  Step 4a: FORMATTING FIX PROMPT       → auto-fix formatting issues found by ATS checker
  Step 4b: LOW SCORE GUIDANCE PROMPT   → improvement roadmap when score too low
"""

# ── THRESHOLD ────────────────────────────────────────────────────────────────
REWRITE_THRESHOLD = 50


# ── PROMPT 1: ANALYSIS ───────────────────────────────────────────────────────

def build_analysis_prompt(job_description: str, resume_text: str) -> str:
    return f"""
You are a senior technical recruiter and ATS specialist with 15 years of experience at FAANG companies.

Your task is to deeply analyze how well a candidate's resume matches a job description.

---

## JOB DESCRIPTION:
{job_description}

---

## CANDIDATE'S RESUME:
{resume_text}

---

## YOUR ANALYSIS TASK:

Evaluate the resume strictly against the job description. Be honest — do not inflate the score.

Score the match from 0 to 100 using this rubric:
- 0–30   : Poor match. Candidate lacks most required skills/experience.
- 31–49  : Weak match. Some overlap but significant gaps exist.
- 50–69  : Moderate match. Core skills present but missing key requirements.
- 70–84  : Good match. Most requirements met with minor gaps.
- 85–100 : Strong match. Resume aligns closely with the role.

---

## RULES:
- Only evaluate what is actually in the resume. Never assume skills not mentioned.
- Be specific — name the actual skills/tools/technologies that are matched or missing.
- The "suggestions" field should give CONCRETE advice the candidate can act on.
- Do NOT rewrite or modify the resume in this step.

---

## OUTPUT FORMAT:
Return a single valid JSON object. No markdown fences, no extra text — just the JSON.

{{
  "score": <integer 0-100>,
  "match_level": "<Poor | Weak | Moderate | Good | Strong>",
  "score_rationale": "<2-3 sentences explaining WHY this score was given>",
  "matched_skills": [
    "<skill or experience that matches>",
    "<skill or experience that matches>"
  ],
  "missing_critical": [
    "<required skill/experience completely absent from resume>",
    "<required skill/experience completely absent from resume>"
  ],
  "missing_preferred": [
    "<preferred/nice-to-have skill that is absent>",
    "<preferred/nice-to-have skill that is absent>"
  ],
  "ats_keywords_to_add": [
    "<keyword from JD not present in resume>",
    "<keyword from JD not present in resume>"
  ],
  "suggestions": [
    "<Specific, actionable improvement #1 the candidate should make to their resume or skills>",
    "<Specific, actionable improvement #2>",
    "<Specific, actionable improvement #3>"
  ],
  "proceed_with_rewrite": <true if score >= {REWRITE_THRESHOLD} else false>
}}
"""


# ── PROMPT 2: REWRITE WITH CONTEXT ───────────────────────────────────────────
# Main rewrite prompt — includes clarifications from user + confirmed extra skills.
# This is the primary rewrite used by the bot.

def build_rewrite_with_context_prompt(
    job_description: str,
    resume_text: str,
    analysis: dict,
    clarifications: dict = None,
    extra_skills: list  = None,
    merged_resume_text: str = "",   # for update path (no JD)
    total_experience: str = "",     # for update path
) -> str:
    """
    Combined rewrite prompt — handles both paths:
    - Job-targeted rewrite: pass job_description + resume_text + analysis
    - General update rewrite: pass merged_resume_text + total_experience
    """
    if clarifications is None: clarifications = {}
    if extra_skills   is None: extra_skills   = []

    # Decide which resume text to use
    base_resume = merged_resume_text if merged_resume_text else resume_text
    has_jd      = bool(job_description and job_description.strip())

    matched      = "\n".join(f"  - {s}" for s in analysis.get("matched_skills", []))
    missing_crit = "\n".join(f"  - {s}" for s in analysis.get("missing_critical", []))
    ats_keywords = "\n".join(f"  - {s}" for s in analysis.get("ats_keywords_to_add", []))
    score        = analysis.get("score", "N/A")

    jd_section = f"""## JOB DESCRIPTION:
{job_description}

## ANALYSIS:
Score: {score}/100
Matched skills: {matched}
Critical gaps: {missing_crit}
ATS keywords to add: {ats_keywords}
""" if has_jd else ""

    clarification_text = ""
    if clarifications:
        clarification_text = "\n## CANDIDATE CLARIFICATIONS (use to enrich resume):\n"
        for q, a in clarifications.items():
            clarification_text += f"Q: {q}\nA: {a}\n\n"

    extra_skills_text = ""
    if extra_skills:
        extra_skills_text = (
            "\n## CONFIRMED EXTRA SKILLS (candidate has these — ADD to skills section):\n"
            + "\n".join(f"  - {s}" for s in extra_skills)
        )

    exp_instruction = ""
    if total_experience:
        exp_instruction = f"\n## EXPERIENCE: Total experience is {total_experience} — USE THIS EXACT VALUE in Summary. Do NOT recalculate.\n"
    elif not has_jd:
        exp_instruction = "\n## EXPERIENCE: Use whatever experience duration is stated in the resume. Do NOT recalculate.\n"

    rewrite_goal = (
        "Rewrite this resume to be perfectly tailored for the job above."
        if has_jd else
        "Improve the writing quality of this resume. Dates and facts are final — only improve how they are written."
    )

    import re as _re

    # Extract institution/company names from resume to explicitly protect them
    # From bold markdown headers: **Company Name | Role | Dates**
    protected_names = _re.findall(r'\*\*([^|*\n]+?)\s*\|', base_resume)
    # From plain-text lines followed by a degree/role/date line (unformatted education/experience)
    protected_names += _re.findall(
        r'^([A-Z][A-Za-z\s&.,()/-]{3,60})\n(?:[A-Z]|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))',
        base_resume, _re.MULTILINE
    )
    protected_names = list(set(n.strip() for n in protected_names if len(n.strip()) > 2))
    names_block = ""
    if protected_names:
        names_list = "\n".join(f"  - \"{n}\" → must appear EXACTLY as \"{n}\"" for n in protected_names[:15])
        names_block = f"""
⚠️ CRITICAL — COPY THESE NAMES EXACTLY (do not shorten, abbreviate, or alter):
{names_list}
"""

    # Extract the EDUCATION section verbatim from source resume to protect it fully
    edu_match = _re.search(
        r'##\s*EDUCATION\s*\n(.*?)(?=\n##|\Z)', base_resume, _re.DOTALL | _re.IGNORECASE
    )
    education_block = ""
    if edu_match:
        edu_raw = edu_match.group(1).strip()
        if edu_raw:
            education_block = f"""
⚠️ EDUCATION — paste this VERBATIM into the EDUCATION section, change nothing:
## EDUCATION
{edu_raw}
"""

    return f"""
You are a senior resume optimization specialist with 15+ years of experience placing candidates at FAANG/MAANG companies. You have deep expertise in Applicant Tracking Systems — how they parse, score, and rank resumes. {rewrite_goal}
{names_block}{education_block}
{jd_section}
## RESUME:
{base_resume}
{exp_instruction}
{clarification_text}
{extra_skills_text}

## RULES — READ EVERY RULE:

### RULE 1 — NEVER FABRICATE
- Only use facts from the resume OR confirmed in clarifications
- NEVER invent metrics, dates, companies, degrees, or skills not present
- NEVER shorten or abbreviate names — "IIT Hyderabad" stays "IIT Hyderabad",
  "Chandigarh Engineering College" stays exactly as written
- PRESERVE every institution name, company name, university name exactly

### RULE 2 — DATES ARE FINAL
- Copy ALL dates exactly as they appear — never change, recalculate, or reformat
- If total_experience is given above — use it verbatim in Summary
- Education dates must be copied exactly from the resume

### RULE 3 — FAANG BULLET FORMULA (mandatory for every bullet)
Formula: [Action Verb] + [What + Tools] + [Scale] + [Measurable Outcome]

SCALE INDICATORS — FAANG recruiters specifically scan for these:
- User/transaction scale: "serving 2M+ daily users", "processing 100K TPS"
- Data scale: "petabyte-scale", "500M+ records"
- Org scope: "across 5 engineering teams", "100+ engineers"

Examples:
WEAK:   "Worked on improving dashboard performance"
STRONG: "Optimized PostgreSQL queries using connection pooling, reducing API latency by 40%
         across 500K DAUs, cutting infra costs by $10K/month"

WEAK:   "Responsible for fraud detection models"
STRONG: "Engineered XGBoost fraud pipeline processing 2M+ daily transactions,
         reducing false positives by 35% and saving $3.5M annually"

Rules:
- Start EVERY bullet with strong action verb (Architected, Engineered, Automated, Reduced,
  Delivered, Spearheaded, Built, Scaled, Optimized, Deployed, Led, Drove, Improved)
- Include the TOOL/HOW — name Python, XGBoost, RAG, Kubernetes, etc.
- Include SCALE — users, requests, transactions, team size
- Include OUTCOME — %, $, ms, time saved — only metrics already in resume
- NEVER say "Responsible for"
- 1-2 lines max per bullet, no first-person pronouns
- Spell out acronyms first time: "Retrieval-Augmented Generation (RAG)", then use RAG

### RULE 4 — KEYWORD STRATEGY
- Mirror JD language EXACTLY — if JD says "low-latency systems" use that phrase not "high-performance systems"
- ATS does exact phrase matching — synonyms often fail
- Include keywords in BOTH skills section AND woven into bullets (double signal = higher ATS score)
- Include both acronym AND full term at least once: "Retrieval-Augmented Generation (RAG)" then use RAG
- Never keyword stuff — every keyword must appear in a meaningful achievement context

### RULE 5 — REUSE FRAMEWORK (layered on top of FAANG formula)
   R — RELEVANCE (only if JD provided):
   - Mirror exact JD language in bullets
   - Reorder bullets: most JD-relevant first within each role
   U — UNDERSTANDING: Show WHY it mattered to the business
   S — SPECIFICITY: Name tool, scope, timeframe, scale — zero vague words
   E — EFFECTIVENESS: Achievement not duty — measurable outcome required

### RULE 5b — COMPANY-SPECIFIC SIGNALS (if JD company is identifiable)
   Google:    Emphasize scale (billions of users), algorithmic thinking, data-driven decisions, autonomous impact
   Meta:      Emphasize speed of execution, growth metrics, high ownership, "move fast" results
   Amazon:    Frame achievements using Leadership Principles (customer obsession, deliver results, ownership)
   Apple:     Highlight craft, polish, attention to detail, user-facing quality metrics
   Netflix:   Emphasize self-direction, high responsibility, independent decision-making, context not control
   Microsoft: Emphasize collaboration, strategic thinking, cross-team impact, platform scale

### RULE 5 — ATS SECTION NAMES (use EXACTLY these, no variations)
   CORRECT           NEVER USE
   -------           ----------
   SUMMARY         ← PROFILE, OBJECTIVE, ABOUT ME
   EXPERIENCE      ← PROFESSIONAL EXPERIENCE, WORK EXPERIENCE, WORK HISTORY
   SKILLS          ← TECHNICAL SKILLS, CORE COMPETENCIES, KEY SKILLS
   EDUCATION       ← ACADEMIC BACKGROUND, QUALIFICATIONS
   PROJECTS        ← KEY PROJECTS, NOTABLE PROJECTS
   ACHIEVEMENTS    ← PATENTS & AWARDS, AWARDS & RECOGNITION, HONORS
   CERTIFICATIONS  ← COURSES, TRAINING

### RULE 6 — FAANG SECTION ORDER (most signal-dense first, bias-triggering last)
1. SUMMARY    — 2-3 punchy lines, years of experience + top achievement + core stack
2. EXPERIENCE — most recruiter time spent here, sell hard
3. SKILLS     — categorized, tailored to JD keywords
4. PROJECTS   — top 2-3 high-signal projects with tech stack
5. EDUCATION  — last (reduces bias), bare minimum: university | degree | years
               - NO GPA unless explicitly 3.9+ and <3 years experience
               - NO percentage, no CGPA unless exceptional
6. ACHIEVEMENTS / CERTIFICATIONS — only if highly relevant (patents, top awards)

### RULE 7 — FORMAT (PDF generator depends on these exactly)
- FIRST LINE must be: # FULL NAME  (H1 markdown — NEVER write "RESUME" or "CV" as a title)
  Example: # DEEPAK KUMAR
- Second line: contact line — email | phone | linkedin (no address, no +91 prefix)
- SECTION HEADERS: every section MUST use "## " prefix (two hashes + space)
  CORRECT:   ## SUMMARY      ## EXPERIENCE      ## SKILLS      ## PROJECTS      ## EDUCATION      ## ACHIEVEMENTS
  WRONG:     SUMMARY         EXPERIENCE         SKILLS         (no ## = PDF breaks completely)
  WRONG:     **EXPERIENCE**  ### EXPERIENCE     *EXPERIENCE*   (only ## works)
  ALL section names must be ALL-CAPS after ## : ## SUMMARY, ## EXPERIENCE, ## SKILLS, ## PROJECTS, ## EDUCATION, ## ACHIEVEMENTS
- Bullets: MUST start with "- " (hyphen space). NEVER "* " or "•"
- NO trailing asterisks anywhere
- Experience entry: **Company | Role | Mon YYYY - Mon YYYY** — ONE line, no location
- Education entry: **Full University Name | Full Degree Name | Mon YYYY - Mon YYYY**
  Example: **IIT Hyderabad | MTech in Data Science | Aug 2025 - May 2027**
  Example: **Chandigarh Engineering College | B.Tech, Computer Science | Aug 2016 - May 2020**
  CRITICAL: Copy institution name EXACTLY — "IIT Hyderabad" NOT "IIT", never shorten
  CRITICAL: Copy dates with month names — "Aug 2025 - May 2027" NOT "2025 - 2027"
- Skills: **Category:** skill1, skill2, skill3 (flat, no nested sections)
- Max 2 lines per bullet, no tables, no columns, no text boxes, single column only

### RULE 8 — PAGE LIMIT
- 2 pages MAX — never sacrifice impactful bullets just to fit 1 page
- 1 page only if candidate has <3 years experience OR <3 roles
- For FAANG specifically: many recruiters prefer a tight 1-page even for senior roles — prioritize density over completeness
- Every line must earn its place — no fluff, no filler

### RULE 9 — WHAT TO OMIT (bias reduction + ATS safety)
- NO home address, street, or full city/state — just country if needed
- NO photo, no graphics, no icons
- NO social media links except LinkedIn and GitHub
- NO LeetCode, CodeChef, HackerRank profile links
- NO GPA/percentage unless exceptional (3.9+ or equivalent)
- NO "References available upon request"
- NO vague skills: "hardworking", "team player", "fast learner"
- NO self-rating bars or proficiency levels (Expert/Intermediate) — prove skill through bullets instead
- NO tools everyone uses: VSCode, Git (unless role-specific)
- NO fluff bullets added just to fill space

## OUTPUT FORMAT — single valid JSON, no markdown fences:
{{
  "optimized_resume_text": "<full rewritten resume>",
  "changes_made": ["<change 1>", "<change 2>", "<change 3>"],
  "final_score_estimate": <integer — estimated ATS score, 0 if no JD>,
  "cover_letter_hook": "<2 punchy sentences for this role, empty string if no JD>",
  "missing_keywords": ["<JD keyword not yet in resume>"],
  "total_experience": "<e.g. 5 years 7 months — only if recalculated, else empty string>"
}}
"""


def build_update_rewrite_prompt(merged_resume_text: str, total_experience: str = "") -> str:
    """Wrapper for update path — calls combined prompt with no JD."""
    return build_rewrite_with_context_prompt(
        job_description="",
        resume_text="",
        analysis={},
        merged_resume_text=merged_resume_text,
        total_experience=total_experience,
    )


def build_rewrite_prompt(job_description: str, resume_text: str, analysis: dict) -> str:
    """Wrapper for agent.py — calls combined prompt with no clarifications."""
    return build_rewrite_with_context_prompt(
        job_description=job_description,
        resume_text=resume_text,
        analysis=analysis,
    )


def build_format_verify_prompt(resume_text: str, protected_names: list) -> str:
    """
    Combined Step 2+3 — fixes FORMAT violations AND verifies names/quality in ONE call.
    Returns patches (find/replace pairs) applied inline — halves latency vs two separate calls.
    """
    names = "\n".join(f'  - "{n}"' for n in protected_names) if protected_names else "  (none extracted)"
    return f"""You are a resume formatter and quality checker. In ONE pass, find and fix ALL violations below.
Do NOT change content, bullets, metrics, or wording — only fix structure and quality issues.

PROTECTED NAMES — must appear EXACTLY as listed, never shorten or abbreviate:
{names}

RESUME:
{resume_text}

CHECK ALL OF THESE:

FORMAT RULES:
1. CONTACT LINE: email | phone | linkedin.com/in/handle  (no +91 prefix, no address)
2. EXPERIENCE: **Company | Role | Mon YYYY - Mon YYYY**  (one bold line, no location line)
3. EDUCATION: **Full Institution Name | Degree | Mon YYYY - Mon YYYY**  (never shorten names)
4. SKILLS: **Category:** skill1, skill2  (bold label with colon)
5. BULLETS: start with "- " (hyphen space), not "* " or "•"
6. SECTIONS: ## SUMMARY  ## EXPERIENCE  ## SKILLS  ## EDUCATION  ## CERTIFICATIONS  ## ACHIEVEMENTS

QUALITY RULES:
7. Institution/company names must match PROTECTED NAMES exactly — never abbreviated
8. No placeholder text: "(metric needed)", "(needs metric)", "[metric needed]" etc.
9. Experience and education headers must be ONE bold line (not split across multiple lines)

Find ALL lines that violate any rule above.
Return a single patch list covering every fix needed.
If nothing needs fixing, return empty patches array.

Return JSON only:
{{
  "patches": [
    {{"find": "<exact existing text>", "replace": "<corrected text>"}},
    ...
  ],
  "fixes": ["<short description of fix 1>", ...]
}}

CRITICAL: "find" must be the EXACT text as it appears in the resume — copy it character for character."""


# ── PROMPT 3: LOW SCORE GUIDANCE ─────────────────────────────────────────────
# Used when score < REWRITE_THRESHOLD.

def build_low_score_guidance_prompt(
    job_description: str,
    resume_text: str,
    analysis: dict,
) -> str:
    score       = analysis.get("score", 0)
    match_level = analysis.get("match_level", "Poor")
    missing     = "\n".join(f"  - {s}" for s in analysis.get("missing_critical", []))
    suggestions = "\n".join(f"  - {s}" for s in analysis.get("suggestions", []))

    return f"""
You are a senior career coach. A candidate applied for a job but their resume score is {score}/100 ({match_level}).

The gap is too large to simply rewrite the resume — they need a development plan first.

---

## JOB DESCRIPTION:
{job_description}

---

## CANDIDATE'S RESUME:
{resume_text}

---

## ANALYSIS SUMMARY:
Score: {score}/100 ({match_level})

Critical missing requirements:
{missing}

Initial suggestions:
{suggestions}

---

## YOUR TASK:
Give the candidate an honest, structured, actionable roadmap to become a strong candidate.
Be encouraging but realistic.

Return a single valid JSON object. No markdown fences, no extra text.

{{
  "honest_assessment": "<2-3 sentences being direct about the gap and what it means>",
  "estimated_time_to_ready": "<e.g. '2-3 months with focused effort'>",
  "skill_gap_roadmap": [
    {{
      "skill": "<missing critical skill>",
      "why_important": "<why this matters for the role>",
      "how_to_learn": "<specific resource: course name, project idea, etc.>",
      "timeframe": "<e.g. 2 weeks>"
    }}
  ],
  "quick_wins": [
    "<thing they can fix on their resume TODAY>",
    "<another quick win>"
  ],
  "alternative_roles": [
    "<similar but more entry-level role they ARE a good fit for now>",
    "<another alternative>"
  ],
  "encouragement": "<1-2 sentences of genuine encouragement with a specific strength>"
}}
"""


# ── PROMPT 4: FORMATTING FIX RERUN ───────────────────────────────────────────
# Called when ATS checker finds formatting issues (not skill gaps).
# Fixes presentation only — never changes substance.

def build_formatting_fix_prompt(
    optimized_resume_text: str,
    job_description: str,
    formatting_issues: list,
    ats_score: int,
) -> str:
    issues_text = "\n".join(f"  - {i}" for i in formatting_issues)

    return f"""
You are an expert resume formatter specializing in ATS optimization.

A resume was just generated and scored {ats_score}/100 by an ATS checker.
The ATS identified these FORMATTING issues (not skill gaps — the content is good):

{issues_text}

Your job is to fix ONLY the formatting and presentation issues.
DO NOT change the substance, skills, companies, degrees, or achievements.
DO NOT add or remove any experience.

## CURRENT RESUME:
{optimized_resume_text}

## JOB DESCRIPTION (for keyword context):
{job_description[:2000]}

## WHAT TO FIX — apply REUSE framework to every bullet:

FORMATTING FIXES:
- Date format → standardize ALL dates to "Mon YYYY - Mon YYYY" (consistent throughout)
- Ordering → most recent experience FIRST (reverse-chronological)
- Asterisks/bad chars → remove any * or ** appearing as literal text
- Bullet format → MUST start with "- " (hyphen space)
- Experience → **Company | Role | Date** on ONE line, no location
- Bullet length → max 2 lines, split anything longer

BULLET FORMULA — apply to every bullet (STAR or XYZ):
- STAR: [Action] what you did → how you did it → measurable result
- XYZ:  "Accomplished X as measured by Y, by doing Z"
- Every bullet needs a metric: %, $, users, time, team size

REUSE FRAMEWORK — apply to every bullet:
- R (Relevance): if JD provided, rewrite bullets using JD keywords naturally
- E (Evidence): start with strong verb (Architected, Led, Reduced, Delivered, Scaled)
                quantify every achievement (%, $, team size, time saved)
                replace "Responsible for" with an action verb + result
- U (Understanding): [Action] + [What/How] + [Result] format on every bullet
- S (Specificity): name the tool, metric, timeframe — no vague filler words
- E (Effectiveness): achievement not duty — "what was the outcome?"

## CRITICAL FORMAT RULES (same as original):
- Bullet points MUST start with "- " (hyphen space). NEVER use "* " or "• "
- NO trailing asterisks anywhere
- Dates: "Mon YYYY - Mon YYYY" only
- Experience: **Company | Role | Date** on one line — NEVER split

## STANDARD ATS SECTION NAMES — MANDATORY:
Use ONLY these exact section names (ATS systems reject non-standard names):

   CORRECT                    NEVER USE
   -------                    ----------
   SKILLS                  ← TECHNICAL SKILLS, CORE COMPETENCIES, KEY SKILLS
   EXPERIENCE              ← PROFESSIONAL EXPERIENCE, WORK EXPERIENCE, WORK HISTORY  
   EDUCATION               ← ACADEMIC BACKGROUND, QUALIFICATIONS
   PROJECTS                ← KEY PROJECTS, NOTABLE PROJECTS
   ACHIEVEMENTS            ← PATENTS & AWARDS, AWARDS & RECOGNITION, HONORS
   CERTIFICATIONS          ← COURSES, TRAINING (keep as CERTIFICATIONS)
   SUMMARY                 ← PROFILE, OBJECTIVE, ABOUT ME

SKILLS section rules:
   - Section header must be exactly: ## SKILLS
   - Keep category labels on each row: **Category:** skill1, skill2
   - Merge all skill subcategories (Domain Expertise, Tools, etc.) under ## SKILLS
   - No nested subsections — flat list of labelled rows only
   - Example:
     ## SKILLS
     **Machine Learning:** XGBoost, LightGBM, Random Forest, NLP, LLMs
     **Programming:** Python, SQL, PySpark, Scikit-Learn, TensorFlow
     **Domain:** Sales Forecasting, Supply Chain, Decision Science

ACHIEVEMENTS section rules:
   - Use ## ACHIEVEMENTS for awards, patents, recognitions
   - Each item as a bullet: - Achievement name (Year) - Issuer


## OUTPUT FORMAT — two blocks separated by the exact delimiter, no markdown fences:

First block: single-line JSON with metadata only (NO resume text here):
{{"fixes_applied": ["fix1", "fix2"], "expected_score_improvement": <integer>}}

Then on the very next line, output exactly:
---RESUME---

Then the full fixed resume in markdown (no JSON wrapping).
"""




# ── PROMPT 5: PROFILE EXTRACTION ─────────────────────────────────────────────
# Called immediately after resume parse to auto-fill the Profile tab.

def build_profile_extract_prompt(resume_text: str) -> str:
    return f"""You are a resume parser. Extract contact and professional details from the resume below.

RESUME:
{resume_text}

Return ONLY a single valid JSON object. No markdown fences, no extra text.
For any field not found in the resume, return an empty string "".
For years_experience, return a short string like "4 years" or "7+ years" based on career span — do NOT guess if not calculable.
For linkedin/github, return just the URL or handle found (e.g. "linkedin.com/in/deepak" or "github.com/deepak").

{{
  "full_name":        "<candidate full name>",
  "email":            "<email address>",
  "phone":            "<phone number with country code if present>",
  "city":             "<city — omit street/state>",
  "country":          "<country if determinable from the resume, e.g. India>",
  "linkedin":         "<linkedin URL or handle, empty if not found>",
  "github":           "<github URL or handle, empty if not found>",
  "portfolio":        "<personal website/portfolio URL, empty if not found>",
  "current_title":    "<most recent job title>",
  "current_company":  "<most recent employer>",
  "years_experience": "<e.g. 4 years, derived from career span>",
  "degree":           "<MOST RECENT/highest degree, e.g. M.Tech>",
  "university":       "<institution for the most recent degree>",
  "graduation_year":  "<end year of the most recent degree>",
  "cgpa":             "<CGPA / GPA / percentage if present, empty if not found>",
  "education": [
    {{"degree": "<e.g. M.Tech>", "field": "<e.g. Data Science>", "institution": "<college/university>", "start_year": "<YYYY>", "end_year": "<YYYY or 'present'>"}}
  ]
}}

List EVERY degree in "education" (most recent first) — include in-progress ones. Capture ALL of them, not just the highest.
"""


# ── End of prompts ────────────────────────────────────────────────────────────