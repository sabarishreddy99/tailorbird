---
name: resume-tailoring
description: Use when creating tailored resumes for job applications - researches company/role, creates optimized templates, conducts branching experience discovery to surface undocumented skills, and generates professional multi-format resumes from user's resume library while maintaining factual integrity
---

# Resume Tailoring Skill

## Overview

Generates high-quality, tailored resumes optimized for specific job descriptions while maintaining factual integrity. Builds resumes around the holistic person by surfacing undocumented experiences through conversational discovery.

**Core Principle:** Truth-preserving optimization - maximize fit while maintaining factual integrity. Never fabricate experience, but intelligently reframe and emphasize relevant aspects.

**Mission:** A person's ability to get a job should be based on their experiences and capabilities, not on their resume writing skills.

## When to Use

Use this skill when:
- User provides a job description and wants a tailored resume
- User has multiple existing resumes in markdown format
- User wants to optimize their application for a specific role/company
- User needs help surfacing and articulating undocumented experiences

**DO NOT use for:**
- Generic resume writing from scratch (user needs existing resume library)
- Cover letters (different skill)
- LinkedIn profile optimization (different skill)

## Quick Start

**Required from user:**
1. Job description (text or URL)
2. Resume library location (defaults to `resumes/` in current directory)

**Workflow:**
1. Build library from existing resumes
2. Research company/role
3. Create template (with user checkpoint)
4. Optional: Branching experience discovery
5. Match content with confidence scoring
6. Generate MD + DOCX + PDF + Report
7. User review → Optional library update

## Implementation

See supporting files:
- `research-prompts.md` - Structured prompts for company/role research
- `matching-strategies.md` - Content matching algorithms and scoring
- `branching-questions.md` - Experience discovery conversation patterns

## Workflow Details

### Multi-Job Detection

**Triggers when user provides:**
- Multiple JD URLs (comma or newline separated)
- Phrases: "multiple jobs", "several positions", "batch", "3 jobs"
- List of companies/roles: "Microsoft PM, Google TPM, AWS PM"

**Detection Logic:**

```python
# Pseudo-code
def detect_multi_job(user_input):
    indicators = [
        len(extract_urls(user_input)) > 1,
        any(phrase in user_input.lower() for phrase in
            ["multiple jobs", "several positions", "batch of", "3 jobs", "5 jobs"]),
        count_company_mentions(user_input) > 1
    ]
    return any(indicators)
```

**If detected:**
```
"I see you have multiple job applications. Would you like to use
multi-job mode?

BENEFITS:
- Shared experience discovery (faster - ask questions once for all jobs)
- Batch processing with progress tracking
- Incremental additions (add more jobs later)

TIME COMPARISON (3 similar jobs):
- Sequential single-job: ~45 minutes (15 min × 3)
- Multi-job mode: ~40 minutes (15 min discovery + 8 min per job)

Use multi-job mode? (Y/N)"
```

**If user confirms Y:**
- Use multi-job workflow (see multi-job-workflow.md)

**If user confirms N or single job detected:**
- Use existing single-job workflow (Phase 0 onwards)

**Backward Compatibility:** Single-job workflow completely unchanged.

**Multi-Job Workflow:**

When multi-job mode is activated, see `multi-job-workflow.md` for complete workflow.

**High-Level Multi-Job Process:**

```
┌─────────────────────────────────────────────────────────────┐
│ PHASE 0: Intake & Batch Initialization                      │
│ - Collect 3-5 job descriptions                              │
│ - Initialize batch structure                                │
│ - Run library initialization (once)                         │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│ PHASE 1: Aggregate Gap Analysis                            │
│ - Extract requirements from all JDs                         │
│ - Cross-reference against library                           │
│ - Build unified gap map (deduplicate)                       │
│ - Prioritize: Critical → Important → Job-specific          │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│ PHASE 2: Shared Experience Discovery                       │
│ - Single branching interview covering ALL gaps              │
│ - Multi-job context for each question                       │
│ - Tag experiences with job relevance                        │
│ - Enrich library with discoveries                           │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│ PHASE 3: Per-Job Processing (Sequential)                   │
│ For each job:                                               │
│   ├─ Research (company + role benchmarking)                 │
│   ├─ Template generation                                    │
│   ├─ Content matching (uses enriched library)              │
│   └─ Generation (MD + DOCX + Report)                        │
│ Interactive or Express mode                                 │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│ PHASE 4: Batch Finalization                                │
│ - Generate batch summary                                    │
│ - User reviews all resumes together                         │
│ - Approve/revise individual or batch                        │
│ - Update library with approved resumes                      │
└─────────────────────────────────────────────────────────────┘
```

**Time Savings:**
- 3 jobs: ~40 min (vs 45 min sequential) = 11% savings
- 5 jobs: ~55 min (vs 75 min sequential) = 27% savings

**Quality:** Same depth as single-job workflow (research, matching, generation)

**See `multi-job-workflow.md` for complete implementation details.**

### Token Optimization & Pattern Reuse (READ FIRST)

**The library is self-improving. Solved patterns MUST be reused, not re-derived.**

Three persistent artifacts make sessions fast and token-cheap:

1. **`{resume_library}/_index/LIBRARY.md`** — the compact knowledge base:
   candidate facts, role-archetype → nearest-resume map, all discovered
   experiences (with truthfulness boundaries), style rules, canonical
   metrics bank, summary variants, skills blocks, research shortcuts,
   and application history.

2. **`{resume_library}/_assets/build_resume.py`** — one-command generator:
   `python3 {resume_library}/_assets/build_resume.py <resume.md> [--outdir DIR]`
   parses the canonical MD format and produces the PDF (auto-fits to
   1 page via line-height retry, warns if content must be trimmed) and
   a validated DOCX. **NEVER hand-author per-resume HTML or DOCX
   generation scripts** — author only the resume .md.

3. **`{repo_root}/job_queue.json`** + **`{repo_root}/tracker/`** — the
   application tracker. `job_queue.json` is the live status of every
   application (one record per role: date, company, role, coverage, status,
   url, notes). The user edits it through a local UI (`python3
   tracker/tracker.py`, browser at :8765); the skill writes to it with
   `tracker/update_queue.py`. LIBRARY.md holds the durable knowledge, the
   queue holds the lifecycle. Both get updated, never just one.

**Token-efficient session flow:**
```
1. Read _index/LIBRARY.md (NOT every resume in the library)
   If the user asks "what's pending / what's next", read job_queue.json
   and filter status == "queued" instead of asking them to re-paste URLs
2. Match the JD to a role archetype from the index table
3. Read ONLY the nearest-archetype resume as the delta-edit base
4. Skip discovery questions the index already answers; ask only
   about genuinely new gaps, and respect truthfulness boundaries
5. Author the new resume .md as a DELTA of the base resume
6. Generate outputs with build_resume.py (one command)
7. On save, APPEND new discoveries/variants/history to _index/LIBRARY.md
   AND upsert the tracker row via tracker/update_queue.py (see Phase 5 step 2b).
   On drop, record in BOTH as well (see the drop block in Phase 1.0).
```

If `_index/LIBRARY.md` does not exist, fall back to the full Phase 0
scan below, then CREATE the index so the next session is cheap.

**What the optimization does NOT compress — research quality:**

The index caches CANDIDATE-side knowledge (experiences, variants,
boundaries). It never substitutes for ROLE-side research. Phase 1
research runs fresh for every job, and you should EXPAND it beyond the
JD whenever judgment says a gap or uncertainty exists:

- ALWAYS: parse the full JD (job-board APIs from the index shortcuts)
  and build the success profile from this specific posting
- EXPAND (WebSearch/WebFetch) when any of these hold:
  * company or team is unfamiliar, or its culture/tech stack materially
    shapes the resume (e.g. leveling systems, named internal teams)
  * the role archetype is new, hybrid, or doesn't cleanly match the
    index table
  * compensation, level, location, or eligibility need verification
  * a requirement is ambiguous and public sources could resolve it
    (engineering blogs, LinkedIn role benchmarking, product docs)
  * projected coverage is below ~80%: research may reveal reframing
    angles or terminology that close the gap
- ALSO: query the user's portfolio/knowledge MCP (if connected) for
  evidence on any NEW requirement theme before asking the user
- The token budget saved on library loading and generation exists
  precisely so research can go DEEPER, not shallower. When unsure
  whether research would help, do it.

### Phase 0: Library Initialization

**Runs first. If `_index/LIBRARY.md` exists, load it and skip the full scan** (read individual resumes only as needed per the archetype table). Otherwise:

**Process:**

1. **Locate resume directory:**
   ```
   User provides path OR default to ./resumes/
   Validate directory exists
   ```

2. **Scan for markdown files:**
   ```
   Use Glob tool: pattern="**/*.md" path={resume_directory}
   (Recursive: the library is organized company-wise, with base resumes
    at the root and company-specific resumes in {Company}/ subfolders.)

   EXCLUDE from resume parsing:
   - *_Report.md files (generation metadata, not resumes)
   - anything under pdfs/

   Count remaining files
   Announce: "Building resume library... found {N} resumes"
   ```

3. **Parse each resume:**
   For each resume file:
   - Use Read tool to load content
   - Extract sections: roles, bullets, skills, education
   - Identify patterns: bullet structure, length, formatting

4. **Build experience database structure:**
   ```json
   {
     "roles": [
       {
         "role_id": "company_title_year",
         "company": "Company Name",
         "title": "Job Title",
         "dates": "YYYY-YYYY",
         "description": "Role summary",
         "bullets": [
           {
             "text": "Full bullet text",
             "themes": ["leadership", "technical"],
             "metrics": ["17x improvement", "$3M revenue"],
             "keywords": ["cross-functional", "program"],
             "source_resumes": ["resume1.md"]
           }
         ]
       }
     ],
     "skills": {
       "technical": ["Python", "Kusto", "AI/ML"],
       "product": ["Roadmap", "Strategy"],
       "leadership": ["Stakeholder mgmt"]
     },
     "education": [...],
     "user_preferences": {
       "typical_length": "1-page|2-page",
       "section_order": ["summary", "experience", "education"],
       "bullet_style": "pattern"
     }
   }
   ```

5. **Tag content automatically:**
   - Themes: Scan for keywords (leadership, technical, analytics, etc.)
   - Metrics: Extract numbers, percentages, dollar amounts
   - Keywords: Frequent technical terms, action verbs

**Output:** In-memory database ready for matching

**Code pattern:**
```python
# Pseudo-code for reference
library = {
    "roles": [],
    "skills": {},
    "education": []
}

for resume_file in glob("resumes/**/*.md"):  # recursive; skip *_Report.md and pdfs/
    content = read(resume_file)
    roles = extract_roles(content)
    for role in roles:
        role["bullets"] = tag_bullets(role["bullets"])
        library["roles"].append(role)

return library
```

### Phase 1: Research Phase

**Goal:** Build comprehensive "success profile" beyond just the job description

**1.0 Eligibility Screen (ALWAYS FIRST, before any research investment):**

Scan the full JD text for work-authorization and eligibility blockers and
flag them to the user UPFRONT, before running research or template phases:

```
HARD BLOCKERS (flag immediately, ask user before proceeding):
- "will not sponsor" / "unable to sponsor" / "no visa sponsorship"
- "must be authorized to work ... without sponsorship now or in the future"
- "US citizenship required" / "US citizens only" / "citizen or permanent
  resident" / "green card required"
- security clearance required (Secret/TS/SCI), ITAR / export-control
  restrictions ("US persons only")
- enrollment requirements the candidate cannot meet (e.g., internships
  requiring current degree enrollment)

PERM-STYLE POSTING DETECTION (flag as likely-low-return):
- Signature pattern: rigid "Bachelor's + N years OR Master's + N-2 years"
  alternatives, exact street address in the JD, "[Multiple Positions
  Available]", flat salary statement, occupation-list phrasing ("or as
  Software Engineer, Software Manager, Lead Software Engineer...")
- These are labor-certification ads often run for a pre-identified
  internal candidate and screened rigidly against stated minimums;
  flag this candidly and check the experience bar STRICTLY (no
  years-framing stretches on formal filing-style requirements)

SOFT FLAGS (mention, usually fine for the candidate's situation):
- "must be currently authorized to work in the US" with NO
  future-sponsorship exclusion (OPT/EAD holders satisfy this today;
  note that sponsorship will be needed later)
- state-residency lists for remote roles (verify candidate's state)
- onsite/hybrid location requiring relocation (confirm willingness)

PRESENTATION:
"⚠️ ELIGIBILITY FLAG: {quoted JD language}
 Impact: {hard blocker | soft flag} for your situation.
 Proceed anyway / drop this role?"

If the JD is silent on authorization, proceed without comment; do not
speculate. Record confirmed decisions (e.g., relocation OK) in
_index/LIBRARY.md so they are not re-asked.
```

**1.0b Years-Ceiling / Level Rule (USER DIRECTIVE — do NOT auto-drop):**

A stated experience CEILING or a junior title is **not**, on its own, a
reason to drop. Apply the pay test first:

```
IF the role's stated pay range TOPS OUT ABOVE $100K:
    → BUILD IT. Do not drop on level alone.
    → Frame years DOWN to the conservative honest figure.
    → Lead with capability, not tenure. Consider OMITTING the years
      count from the summary entirely so the resume reads
      capability-first rather than over- or under-qualified
      (precedent: Apple Cloud AI Platform 2026-07-22).
    → Flag the level mismatch plainly in the report, never on the resume.

IF pay is BELOW $100K, or no range is stated and the title is
clearly entry (Software Engineer I / Associate / New Grad / intern):
    → Flag to the user with the numbers and let THEM decide.
    → Only drop unilaterally when a HARD BLOCKER also applies
      (sponsorship, clearance, or an absent required language/platform).

ALWAYS distinguish a CEILING from a FLOOR before flagging anything:
  - "0-2 years", "1-2 years", "0-4 years"      → ceiling
  - "2+ years", "3+ years", "5+ years"         → FLOOR, being above it
                                                  is a STRENGTH, never flag
  - "1 or more years" on an early-career-framed
    req                                        → floor; build normally
```

Titles that look junior but are not: **"Associate" at Goldman Sachs and
most banks is MID-level** (Analyst → Associate → VP). Check the band
before assuming.

**Board sweep before dropping on level:** when a req is under-levelled,
list the company's other open roles from the SAME API response before
deciding. Ashby, Greenhouse, and Lever all return the full board in one
call. Precedent: a pasted Astronomer req was 0-4 yrs at $144-160K; the
same board carried a Senior role at 5+ yrs and $200-230K.

**WHENEVER A ROLE IS DROPPED (at any phase, for any reason), record it twice:**

```
1. _index/LIBRARY.md Application History: add a row with the drop reason,
   quoting the blocking JD language verbatim when it is an eligibility block.

2. job_queue.json, via the tracker CLI:

   python3 {repo_root}/tracker/update_queue.py \
       --company "{Company}" \
       --role "{Role}" \
       --status dropped \
       --url "{job posting URL}" \
       --notes "{drop reason, e.g. HARD BLOCKER: no sponsorship}"

Do this for drops at ANY phase, including a role killed by the eligibility
screen before research begins. A dropped role that never reaches the tracker
looks unprocessed, and the user will paste the same URL again later.

Do NOT generate resume files for a dropped role. Record and move on.
```

**Inputs:**
- Job description (text or URL from user)
- Optional: Company name if not in JD

**Process:**

**1.1 Job Description Parsing:**
```
Use research-prompts.md JD parsing template
Extract: requirements, keywords, implicit preferences, red flags, role archetype
```

**1.2 Company Research:**
```
WebSearch queries:
- "{company} mission values culture"
- "{company} engineering blog"
- "{company} recent news"

Synthesize: mission, values, business model, stage
```

**1.3 Role Benchmarking:**
```
WebSearch: "site:linkedin.com {job_title} {company}"
WebFetch: Top 3-5 profiles
Analyze: common backgrounds, skills, terminology

If sparse results, try similar companies
```

**1.4 Success Profile Synthesis:**
```
Combine all research into structured profile (see research-prompts.md template)

Include:
- Core requirements (must-have)
- Valued capabilities (nice-to-have)
- Cultural fit signals
- Narrative themes
- Terminology map (user's background → their language)
- Risk factors + mitigations
```

**Checkpoint:**
```
Present success profile to user:

"Based on my research, here's what makes candidates successful for this role:

{SUCCESS_PROFILE_SUMMARY}

Key findings:
- {Finding 1}
- {Finding 2}
- {Finding 3}

Does this match your understanding? Any adjustments?"

Wait for user confirmation before proceeding.
```

**Output:** Validated success profile document

### Phase 2: Template Generation

**Goal:** Create resume structure optimized for this specific role

**Inputs:**
- Success profile (from Phase 1)
- User's resume library (from Phase 0)

**Process:**

**2.1 Analyze User's Resume Library:**
```
Extract from library:
- All roles, titles, companies, date ranges
- Role archetypes (technical contributor, manager, researcher, specialist)
- Experience clusters (what domains/skills appear frequently)
- Career progression and narrative
```

**2.2 Role Consolidation Decision:**

**When to consolidate:**
- Same company, similar responsibilities
- Target role values continuity over granular progression
- Combined narrative stronger than separate
- Page space constrained

**When to keep separate:**
- Different companies (ALWAYS separate)
- Dramatically different responsibilities that both matter
- Target role values specific progression story
- One position has significantly more relevant experience

**Decision template:**
```
For {Company} with {N} positions:

OPTION A (Consolidated):
Title: "{Combined_Title}"
Dates: "{First_Start} - {Last_End}"
Rationale: {Why consolidation makes sense}

OPTION B (Separate):
Position 1: "{Title}" ({Dates})
Position 2: "{Title}" ({Dates})
Rationale: {Why separate makes sense}

RECOMMENDED: Option {A/B} because {reasoning}
```

**2.3 Title Reframing Principles:**

**Core rule:** Stay truthful to what you did, emphasize aspect most relevant to target

**Strategies:**

1. **Emphasize different aspects:**
   - "Graduate Researcher" → "Research Software Engineer" (if coding-heavy)
   - "Data Science Lead" → "Technical Program Manager" (if leadership)

2. **Use industry-standard terminology:**
   - "Scientist III" → "Senior Research Scientist" (clearer seniority)
   - "Program Coordinator" → "Project Manager" (standard term)

3. **Add specialization when truthful:**
   - "Engineer" → "ML Engineer" (if ML work substantial)
   - "Researcher" → "Computational Ecologist" (if computational methods)

4. **Adjust seniority indicators:**
   - "Lead" vs "Senior" vs "Staff" based on scope

**Constraints:**
- NEVER claim work you didn't do
- NEVER inflate seniority beyond defensible
- Company name and dates MUST be exact
- Core responsibilities MUST be accurate

**2.4 Generate Template Structure:**

```markdown
## Professional Summary
[GUIDANCE: {X} sentences emphasizing {themes from success profile}]
[REQUIRED ELEMENTS: {keywords from JD}]

## Key Skills
[STRUCTURE: {2-4 categories based on JD structure}]
[SOURCE: Extract from library matching success profile]

## Professional Experience

### [ROLE 1 - Most Recent/Relevant]
[CONSOLIDATION: {merge X positions OR keep separate}]
[TITLE OPTIONS:
  A: {emphasize aspect 1}
  B: {emphasize aspect 2}
  Recommended: {option with rationale}]
[BULLET ALLOCATION: {N bullets based on relevance + recency}]
[GUIDANCE: Emphasize {themes}, look for {experience types}]

Bullet 1: [SEEKING: {requirement type}]
Bullet 2: [SEEKING: {requirement type}]
...

### [ROLE 2]
...

## Education
[PLACEMENT: {top if required/recent, bottom if experience-heavy}]

## [Optional Sections]
[INCLUDE IF: {criteria from success profile}]
```

**Checkpoint:**
```
Present template to user:

"Here's the optimized resume structure for this role:

STRUCTURE:
{Section order and rationale}

ROLE CONSOLIDATION:
{Decisions with options}

TITLE REFRAMING:
{Proposed titles with alternatives}

BULLET ALLOCATION:
Role 1: {N} bullets (most relevant)
Role 2: {N} bullets
...

Does this structure work? Any adjustments to:
- Role consolidation?
- Title reframing?
- Bullet allocation?"

Wait for user approval before proceeding.
```

**Output:** Approved template skeleton with guidance for each section

### Phase 2.5: Experience Discovery (OPTIONAL)

**Goal:** Surface undocumented experiences through conversational discovery

**When to trigger:**
```
After template approval, if gaps identified:

"I've identified {N} gaps or areas where we have weak matches:
- {Gap 1}: {Current confidence}
- {Gap 2}: {Current confidence}
...

Would you like to do a structured brainstorming session to surface
any experiences you haven't documented yet?

This typically takes 10-15 minutes and often uncovers valuable content."

User can accept or skip.
```

**Branching Interview Process:**

**Approach:** Conversational with follow-up questions based on answers

**For each gap, conduct branching dialogue (see branching-questions.md):**

1. **Start with open probe:**
   - Technical gap: "Have you worked with {skill}?"
   - Soft skill gap: "Tell me about times you've {demonstrated_skill}"
   - Recent work: "What have you worked on recently?"

2. **Branch based on answer:**
   - YES/Strong → Deep dive (scale, challenges, metrics)
   - INDIRECT → Explore role and transferability
   - ADJACENT → Explore related experience
   - PERSONAL → Assess recency and substance
   - NO → Try broader category or move on

3. **Follow-up systematically:**
   - Ask "what," "how," "why" to get details
   - Quantify: "Any metrics?"
   - Contextualize: "Was this production?"
   - Validate: "Does this address the gap?"

4. **Capture immediately:**
   - Document experience as shared
   - Ask clarifying questions (dates, scope, impact)
   - Help articulate as resume bullet
   - Tag which gap(s) it addresses

**Capture Structure:**
```markdown
## Newly Discovered Experiences

### Experience 1: {Brief description}
- Context: {Where/when}
- Scope: {Scale, duration, impact}
- Addresses: {Which gaps}
- Bullet draft: "{Achievement-focused bullet}"
- Confidence: {How well fills gap - percentage}

### Experience 2: ...
```

**Integration Options:**

After discovery session:
```
"Great! I captured {N} new experiences. For each one:

1. ADD TO CURRENT RESUME - Integrate now
2. ADD TO LIBRARY ONLY - Save for future, not needed here
3. REFINE FURTHER - Think more about articulation
4. DISCARD - Not relevant enough

Let me know for each experience."
```

**Important Notes:**
- Keep truthfulness bar high - help articulate, NEVER fabricate
- Focus on gaps and weak matches, not strong areas
- Time-box if needed (10-15 minutes typical)
- User can skip entirely if confident in library
- Recognize when to move on - don't exhaust user

**Output:** New experiences integrated into library, ready for matching

### Phase 3: Assembly Phase

**Goal:** Fill approved template with best-matching content, with transparent scoring

**Inputs:**
- Approved template (from Phase 2)
- Resume library + discovered experiences (from Phase 0 + 2.5)
- Success profile (from Phase 1)

**Process:**

**3.1 For Each Template Slot:**

1. **Extract all candidate bullets from library**
   - All bullets from library database
   - All newly discovered experiences
   - Include source resume for each

2. **Score each candidate** (see matching-strategies.md)
   - Direct match (40%): Keywords, domain, technology, outcome
   - Transferable (30%): Same capability, different context
   - Adjacent (20%): Related tools, methods, problem space
   - Impact (10%): Achievement type alignment

   Overall = (Direct × 0.4) + (Transfer × 0.3) + (Adjacent × 0.2) + (Impact × 0.1)

3. **Rank candidates by score**
   - Sort high to low
   - Group by confidence band:
     * 90-100%: DIRECT
     * 75-89%: TRANSFERABLE
     * 60-74%: ADJACENT
     * <60%: WEAK/GAP

4. **Present top 3 matches with analysis:**
   ```
   TEMPLATE SLOT: {Role} - Bullet {N}
   SEEKING: {Requirement description}

   MATCHES:
   [DIRECT - 95%] "{bullet_text}"
     ✓ Direct: {what matches directly}
     ✓ Transferable: {what transfers}
     ✓ Metrics: {quantified impact}
     Source: {resume_name}

   [TRANSFERABLE - 78%] "{bullet_text}"
     ✓ Transferable: {what transfers}
     ✓ Adjacent: {what's adjacent}
     ⚠ Gap: {what's missing}
     Source: {resume_name}

   [ADJACENT - 62%] "{bullet_text}"
     ✓ Adjacent: {what's related}
     ⚠ Gap: {what's missing}
     Source: {resume_name}

   RECOMMENDATION: Use DIRECT match (95%)
   ALTERNATIVE: If avoiding repetition, use TRANSFERABLE (78%) with reframing
   ```

5. **Handle gaps (confidence <60%):**
   ```
   GAP IDENTIFIED: {Requirement}

   BEST AVAILABLE: {score}% - "{bullet_text}"

   REFRAME OPPORTUNITY: {If applicable}
   Original: "{text}"
   Reframed: "{adjusted_text}" (truthful because {reason})
   New confidence: {score}%

   OPTIONS:
   1. Use reframed version ({new_score}%)
   2. Acknowledge gap in cover letter
   3. Omit bullet slot (reduce allocation)
   4. Use best available with disclosure

   RECOMMENDATION: {Most appropriate option}
   ```

**3.2 Content Reframing:**

When good match (>60%) but terminology misaligned:

**Apply strategies from matching-strategies.md:**
- Keyword alignment (preserve meaning, adjust terms)
- Emphasis shift (same facts, different focus)
- Abstraction level (adjust technical specificity)
- Scale emphasis (highlight relevant aspects)

**Show before/after for transparency:**
```
REFRAMING APPLIED:
Bullet: {template_slot}

Original: "{original_bullet}"
Source: {resume_name}

Reframed: "{reframed_bullet}"
Changes: {what changed and why}
Truthfulness: {why this is accurate}
```

**Checkpoint:**
```
"I've matched content to your template. Here's the complete mapping:

COVERAGE SUMMARY:
- Direct matches: {N} bullets ({percentage}%)
- Transferable: {N} bullets ({percentage}%)
- Adjacent: {N} bullets ({percentage}%)
- Gaps: {N} ({percentage}%)

REFRAMINGS APPLIED: {N}
- {Example 1}
- {Example 2}

GAPS IDENTIFIED:
- {Gap 1}: {Recommendation}
- {Gap 2}: {Recommendation}

OVERALL JD COVERAGE: {percentage}%

Review the detailed mapping below. Any adjustments to:
- Match selections?
- Reframings?
- Gap handling?"

[Present full detailed mapping]

Wait for user approval before generation.
```

**Output:** Complete bullet-by-bullet mapping with confidence scores and reframings

### Phase 4: Generation Phase

**Goal:** Create professional multi-format outputs

**Inputs:**
- Approved content mapping (from Phase 3)
- User's formatting preferences (from library analysis)
- Target role information (from Phase 1)

**Process:**

**4.1 Markdown Generation:**

**Compile mapped content into clean markdown:**

```markdown
# {User_Name}

{Contact_Info}

---

## Professional Summary

{Summary_from_template}

---

## Key Skills

**{Category_1}:**
- {Skills_from_library_matching_profile}

**{Category_2}:**
- {Skills_from_library_matching_profile}

{Repeat for all categories}

---

## Professional Experience

### {Job_Title}
**{Company} | {Location} | {Dates}**

{Role_summary_if_applicable}

• {Bullet_1_from_mapping}
• {Bullet_2_from_mapping}
...

### {Next_Role}
...

---

## Education

**{Degree}** | {Institution} ({Year})
**{Degree}** | {Institution} ({Year})
```

**Use user's preferences:**
- Formatting style from library analysis
- Bullet structure pattern
- Section ordering
- Typical length (1-page vs 2-page)

**Writing style rules (apply to ALL generated content — summary, bullets, project descriptions, across MD/DOCX/PDF):**
- **ONE FULL, TIGHT PAGE — exactly.** Never 2 pages, and never a visibly
  underfilled page. build_resume.py picks the most generous line-height
  that fits and prints an UNDERFILLED note when content is light
  (fits at line-height >= 1.2). When that note appears, ADD relevant
  content rather than shipping a short page, in this priority order:
  1) restore the most JD-relevant trimmed experience entry (e.g., VIT)
  2) add a second project relevant to the JD
  3) restore the coursework line
  4) expand the strongest bullets with truthful detail from the index
  Then re-run the build and confirm the note is gone.
- **NEVER use em dashes (—).** Restructure the sentence instead: use commas, "with", "spanning", "including", "that fuses", or split into two clauses. En dashes (–) remain acceptable in date ranges only.
- **Avoid unnecessary colons in prose.** Rewrite "Sole engineer on an assistant: a 4-stage pipeline..." as "Sole engineer on an assistant with a 4-stage pipeline...". Keep only structural colons: skills category labels ("AI/ML:"), "Coursework:", client annotations ("Client: Shell PLC").
- Before finalizing, scan every output format for "—" and prose colons and fix any found.

**Output:** `{Name}_{Company}_{Role}_Resume.md`

**4.2 + 4.3 DOCX and PDF Generation (single command):**

```
PREFERRED PATH (token-efficient, always try first):

  python3 {resume_library}/_assets/build_resume.py {resume.md} [--outdir DIR]

This parses the canonical resume MD and produces BOTH:
  - {basename}.pdf   (headless Chrome; auto-fits 1 page by retrying
                      line-height 1.13 → 1.10 → 1.07; prints a WARNING
                      if still 2 pages → trim clauses in the MD, re-run)
  - {basename}.docx  (validated docx-js output, Calibri, proper bullets)

After generation:
  - Verify DOCX with the docx skill's validate.py
  - Visually check the PDF's first page:
    qlmanage -t -s 1200 -o /tmp {pdf} → Read the PNG

FALLBACK (only if build_resume.py is missing or fails on a structure
it cannot parse): use document-skills:docx to hand-build the DOCX and
headless Chrome with a print-styled HTML for the PDF, then consider
extending build_resume.py so the next session does not pay this cost.
```

**Output:** `{Name}_{Company}_{Role}_Resume.docx` + `{Name}_{Company}_{Role}_Resume.pdf`

**4.4 Generation Summary Report:**

**Create metadata file:**

```markdown
# Resume Generation Report
**{Role} at {Company}**

**Date Generated:** {timestamp}

## Target Role Summary
- Company: {Company}
- Position: {Role}
- IC Level: {If known}
- Focus Areas: {Key areas}

## Success Profile Summary
- Key Requirements: {top 5}
- Cultural Fit Signals: {themes}
- Risk Factors Addressed: {mitigations}

## Content Mapping Summary
- Total bullets: {N}
- Direct matches: {N} ({percentage}%)
- Transferable: {N} ({percentage}%)
- Adjacent: {N} ({percentage}%)
- Gaps identified: {list}

## Reframing Applied
- {bullet}: {original} → {reframed} [Reason: {why}]
...

## Source Resumes Used
- {resume1}: {N} bullets
- {resume2}: {N} bullets
...

## Gaps Addressed

### Before Experience Discovery:
{Gap analysis showing initial state}

### After Experience Discovery:
{Gap analysis showing final state}

### Remaining Gaps:
{Any unresolved gaps with recommendations}

## Key Differentiators for This Role
{What makes user uniquely qualified}

## Recommendations for Interview Prep
- Stories to prepare
- Questions to expect
- Gaps to address
```

**Output:** `{Name}_{Company}_{Role}_Resume_Report.md`

**Present to user:**
```
"Your tailored resume has been generated!

FILES CREATED:
- {Name}_{Company}_{Role}_Resume.md
- {Name}_{Company}_{Role}_Resume.docx
- {Name}_{Company}_{Role}_Resume_Report.md
{- {Name}_{Company}_{Role}_Resume.pdf (if requested)}

QUALITY METRICS:
- JD Coverage: {percentage}%
- Direct Matches: {percentage}%
- Newly Discovered: {N} experiences

Review the files and let me know:
1. Save to library (recommended)
2. Need revisions
3. Save but don't add to library"
```

### Phase 5: Library Update (CONDITIONAL)

**Goal:** Optionally add successful resume to library for future use

**When:** After user reviews and approves generated resume

**Checkpoint Question:**
```
"Are you satisfied with this resume?

OPTIONS:
1. YES - Save to library
   → Adds resume to permanent location
   → Rebuilds library database
   → Makes new content available for future resumes

2. NO - Need revisions
   → What would you like to adjust?
   → Make changes and re-present

3. SAVE BUT DON'T ADD TO LIBRARY
   → Keep files in current location
   → Don't enrich database
   → Useful for experimental resumes

Which option?"
```

**If Option 1 (YES - Save to library):**

**Process:**

1. **Move resume to library (company-wise organization):**
   ```
   The library is organized by company. Create folders as needed:

   - .md file      → {resume_library}/{Company}/{Name}_{Company}_{Role}_Resume.md
   - .docx file    → {resume_library}/{Company}/{Name}_{Company}_{Role}_Resume.docx
   - _Report.md    → {resume_library}/{Company}/{Name}_{Company}_{Role}_Resume_Report.md
   - .pdf (if any) → {resume_library}/pdfs/{Company}/{Name}_{Company}_{Role}_Resume.pdf

   Resulting layout:
   {resume_library}/
   ├── {base resumes with no target company}.md   (library root)
   ├── {Company}/           (md + docx + report per company)
   └── pdfs/{Company}/      (submission-ready PDFs per company)

   Use the company's plain name for the folder (e.g. Apple, Asana,
   Storable). If a company is applied to more than once, the role in
   the filename keeps files distinct within the same folder.
   ```

2. **Update the persistent index (`_index/LIBRARY.md`) — replaces full rebuild:**
   ```
   Append/update in {resume_library}/_index/LIBRARY.md:
   - Role-archetype table: add or update the row pointing to this resume
   - Discovered Experiences: add anything newly confirmed this session
     (with its truthfulness boundary)
   - Summary Variants / Skills Blocks: add genuinely new reusable variants
   - Application History: add row (date, company, role, coverage, saved)
   - Any new research shortcuts learned (e.g., job-board API patterns)

   Keep the index COMPACT: it is read at the start of every session.
   One line per fact; no duplication of full resume text.
   ```

2b. **Update the application tracker (`job_queue.json`) — REQUIRED, same moment as the index:**
   ```
   python3 {repo_root}/tracker/update_queue.py \
       --company "{Company}" \
       --role "{Role} ({location/comp shorthand})" \
       --status resume_ready \
       --coverage "~{N}%" \
       --url "{job posting URL}" \
       --notes "{one-line why, same gist as the LIBRARY history row}"

   The script upserts, so it is safe to re-run. It matches an existing entry by
   URL first (rows the user queued from the tracker UI have a URL but no role
   yet), then by company. It will NOT regress a status the user advanced
   manually in the UI (e.g. leaves "applied" alone rather than resetting it to
   "resume_ready"); pass --force only if the user explicitly asks.

   Statuses: queued → resume_ready → applied → screen → interviewing →
             offer / rejected / dropped
   The skill only ever sets `resume_ready` or `dropped`. Everything past
   submission is the user's to set in the tracker UI; never set those for them.
   ```

3. **Preserve generation metadata:**
   ```json
   {
     "resume_id": "{Name}_{Company}_{Role}",
     "generated": "{timestamp}",
     "source_resumes": ["{resume1}", "{resume2}"],
     "reframings": [
       {
         "original": "{text}",
         "reframed": "{text}",
         "reason": "{why}"
       }
     ],
     "match_scores": {
       "bullet_1": 95,
       "bullet_2": 87,
       ...
     },
     "newly_discovered": [
       {
         "experience": "{description}",
         "bullet": "{text}",
         "addresses_gap": "{gap}"
       }
     ]
   }
   ```

4. **Announce completion:**
   ```
   "Resume saved to library!

   Library updated:
   - Total resumes: {N}
   - New content variations: {N}
   - Newly discovered experiences added: {N}

   This resume and its new content are now available for future tailoring sessions."
   ```

**If Option 2 (NO - Need revisions):**

```
"What would you like to adjust?"

[Collect user feedback]
[Make requested changes]
[Re-run relevant phases]
[Re-present for approval]

[Repeat until satisfied or user cancels]
```

**If Option 3 (SAVE BUT DON'T ADD TO LIBRARY):**

```
"Resume files saved to current directory:
- {Name}_{Company}_{Role}_Resume.md
- {Name}_{Company}_{Role}_Resume.docx
- {Name}_{Company}_{Role}_Resume_Report.md

Not added to library - you can manually move later if desired."
```

**Benefits of Library Update:**
- Grows library with each successful resume
- New bullet variations become available
- Reframings that work can be reused
- Discovered experiences permanently captured
- Future sessions start with richer library
- Self-improving system over time

**Output:** Updated library database + metadata preservation (if Option 1)

## Error Handling & Edge Cases

**Edge Case 1: Insufficient Resume Library**
```
SCENARIO: User has only 1-2 resumes, limited content

HANDLING:
"⚠️ Limited resume library detected ({N} resumes).

This may result in:
- Fewer matching options
- More gaps in coverage
- Less variety in bullet phrasing

RECOMMENDATIONS:
- Proceed with available content (I'll do my best!)
- Consider adding more resumes after this generation
- Experience Discovery phase will be especially valuable

Continue? (Y/N)"
```

**Edge Case 2: No Good Matches (confidence <60% for critical requirement)**
```
SCENARIO: Template slot requires experience user doesn't have

HANDLING:
"❌ GAP: {Requirement}

No matches found with confidence >60%

OPTIONS:
1. Run Experience Discovery - might surface undocumented work
2. Reframe best available ({score}%) - I'll show you the reframing
3. Omit bullet slot - reduce template allocation
4. Note for cover letter - emphasize learning ability

Which approach?"

[Don't force matches - be transparent about gaps]
```

**Edge Case 3: Research Phase Failures**
```
SCENARIO: WebSearch fails, LinkedIn unavailable, company info sparse

HANDLING:
"⚠️ Limited company research available.

What I found:
- {Available info}

What's missing:
- {Missing areas}

Falling back to job description-only analysis.
Result: Template will be optimized for JD but may miss cultural nuances.

Do you have additional context about:
- Company culture?
- Team structure?
- Technologies used?

(Optional - you can share or skip)"

[Proceed with best-effort approach]
```

**Edge Case 4: Job Description Quality Issues**
```
SCENARIO: Vague JD, missing requirements, poorly written

HANDLING:
"⚠️ Job description lacks detail in areas:
- {Missing area 1}
- {Missing area 2}

This may limit template optimization.

Do you have additional context about:
- {Question 1}
- {Question 2}

(Optional - I'll work with what's available)"

[Extract what's possible, proceed]
```

**Edge Case 5: Ambiguous Role Consolidation**
```
SCENARIO: Unclear whether to merge roles or keep separate

HANDLING:
"🤔 Ambiguous consolidation decision for {Company}

OPTION A (Consolidated): {Rationale}
OPTION B (Separate): {Rationale}

Both are defensible. Which do you prefer?

(This becomes your preference for similar situations)"

[Remember preference for future]
```

**Edge Case 6: Resume Length Constraints**
```
SCENARIO: Too much good content, exceeds 2 pages

HANDLING:
"⚠️ Content exceeds 2 pages (current: {N} bullets, ~{pages} pages)

PRUNING SUGGESTIONS (ranked by relevance):
Remove:
- {Bullet X}: {score}% match (lowest)
- {Bullet Y}: {score}% match
...

Keep all bullets and accept >2 pages?
OR
Remove {N} bullets to fit 2 pages?

Your preference?"

[User decides priority]
```

**Error Recovery:**
- All checkpoints allow going back to previous phase
- User can request adjustments at any checkpoint
- Generation failures (DOCX/PDF) fall back to markdown-only
- Progress saved between phases (can resume if interrupted)

**Graceful Degradation:**
- Research limited → Fall back to JD-only analysis
- Library small → Work with available + emphasize discovery
- Matches weak → Transparent gap identification
- Generation fails → Provide markdown + error details

## Usage Examples

**Example 1: Internal Role (Same Company)**
```
USER: "I want to apply for Principal PM role in 1ES team at Microsoft.
      Here's the JD: {paste}"

SKILL:
1. Library Build: Finds 29 resumes
2. Research: Microsoft 1ES team, internal culture, role benchmarking
3. Template: Features PM2 Azure Eng Systems role (most relevant)
4. Discovery: Surfaces VS Code extension, Bhavana AI side project
5. Assembly: 92% JD coverage, 75% direct matches
6. Generate: MD + DOCX + Report
7. User approves → Library updated with new resume + 6 discovered experiences

RESULT: Highly competitive application leveraging internal experience
```

**Example 2: Career Transition (Different Domain)**
```
USER: "I'm a TPM trying to transition to ecology PM role. JD: {paste}"

SKILL:
1. Library Build: Finds existing TPM resumes
2. Research: Ecology sector, sustainability focus, cross-domain transfers
3. Template: Reframes "Technical Program Manager" → "Program Manager,
             Environmental Systems" emphasizing systems thinking
4. Discovery: Surfaces volunteer conservation work, graduate research in
             environmental modeling
5. Assembly: 65% JD coverage - flags gaps in domain-specific knowledge
6. Generate: Resume + gap analysis with cover letter recommendations

RESULT: Bridges technical skills with environmental domain
```

**Example 3: Career Gap Handling**
```
USER: "I have a 2-year gap while starting a company. JD: {paste}"

SKILL:
1. Library Build: Finds pre-gap resumes
2. Research: Standard analysis
3. Template: Includes startup as legitimate role
4. Discovery: Surfaces skills developed during startup (fundraising,
             product development, team building)
5. Assembly: Frames gap as entrepreneurial experience
6. Generate: Resume presenting gap as valuable experience

RESULT: Gap becomes strength showing initiative and diverse skills
```

**Example 4: Multi-Job Batch (3 Similar Roles)**
```
USER: "I want to apply for these 3 TPM roles:
      1. Microsoft 1ES Principal PM
      2. Google Cloud Senior TPM
      3. AWS Container Services Senior PM
      Here are the JDs: {paste 3 JDs}"

SKILL:
1. Multi-job detection: Triggered (3 JDs detected)
2. Intake: Collects all 3 JDs, initializes batch
3. Library Build: Finds 29 resumes (once)
4. Gap Analysis: Identifies 14 gaps, 8 unique after deduplication
5. Shared Discovery: 30-minute session surfaces 5 new experiences
   - Kubernetes CI/CD for nonprofits
   - Azure migration for university lab
   - Cross-functional team leadership examples
   - Recent hackathon project
   - Open source contributions
6. Per-Job Processing (×3):
   - Job 1 (Microsoft): 85% coverage, emphasizes Azure/1ES alignment
   - Job 2 (Google): 88% coverage, emphasizes technical depth
   - Job 3 (AWS): 78% coverage, addresses AWS gap in cover letter recs
7. Batch Finalization: All 3 resumes reviewed, approved, added to library

RESULT: 3 high-quality resumes in 40 minutes vs 45 minutes sequential
        5 new experiences captured, available for future applications
        Average coverage: 84%, all critical gaps resolved
```

**Example 5: Incremental Batch Addition**
```
WEEK 1:
USER: "I want to apply for 3 jobs: {Microsoft, Google, AWS}"
SKILL: [Processes batch as above, completes in 40 min]

WEEK 2:
USER: "I found 2 more jobs: Stripe and Meta. Add them to my batch?"
SKILL:
1. Load existing batch (includes 5 previously discovered experiences)
2. Intake: Adds Job 4 (Stripe), Job 5 (Meta)
3. Incremental Gap Analysis: Only 3 new gaps (vs 14 original)
   - Payment systems (Stripe-specific)
   - Social networking (Meta-specific)
   - React/frontend (both)
4. Incremental Discovery: 10-minute session for new gaps only
   - Surfaces payment processing side project
   - React work from bootcamp
   - Large-scale system design course
5. Per-Job Processing (×2): Jobs 4, 5 processed independently
6. Updated Batch Summary: Now 5 jobs total, 8 experiences discovered

RESULT: 2 additional resumes in 20 minutes (vs 30 min if starting from scratch)
        Time saved by not re-asking 8 previous gaps: ~20 minutes
```

## Testing Guidelines

**Manual Testing Checklist:**

**Test 1: Happy Path**
```
- Provide JD with clear requirements
- Library with 10+ resumes
- Run all phases without skipping
- Verify generated files
- Check library update
PASS CRITERIA:
- All files generated correctly
- JD coverage >70%
- No errors in any phase
```

**Test 2: Minimal Library**
```
- Provide only 2 resumes
- Run through workflow
- Verify gap handling
PASS CRITERIA:
- Graceful warning about limited library
- Still produces reasonable output
- Gaps clearly identified
```

**Test 3: Research Failures**
```
- Use obscure company with minimal online presence
- Verify fallback to JD-only
PASS CRITERIA:
- Warning about limited research
- Proceeds with JD analysis
- Template still reasonable
```

**Test 4: Experience Discovery Value**
```
- Run with deliberate gaps in library
- Conduct experience discovery
- Verify new experiences integrated
PASS CRITERIA:
- Discovers genuine undocumented experiences
- Integrates into final resume
- Improves JD coverage
```

**Test 5: Title Reframing**
```
- Test various role transitions
- Verify title reframing suggestions
PASS CRITERIA:
- Multiple options provided
- Truthfulness maintained
- Rationales clear
```

**Test 6: Multi-format Generation**
```
- Generate MD, DOCX, PDF, Report
- Verify formatting consistency
PASS CRITERIA:
- All formats readable
- Formatting professional
- Content identical across formats
```

**Regression Testing:**
```
After any SKILL.md changes:
1. Re-run Test 1 (happy path)
2. Verify no functionality broken
3. Commit only if passes
```
