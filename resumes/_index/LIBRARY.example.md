# Resume Library Index (EXAMPLE / TEMPLATE)

This is a sanitized skeleton of `LIBRARY.md`, the candidate knowledge base the agent and
skill read. The real `LIBRARY.md` is git-ignored because it contains personal data
(name, contact, and full application history). Copy this to `LIBRARY.md` and fill it in
to use the system.

## Candidate Facts (verified, stable)

**Full Name** · +1 (000) 000-0000 · you@example.com · linkedin.com/in/you · yoursite.com
Degree, school, dates, GPA. Location. Relocation preference. Work-authorization note.

## Role Archetypes → Nearest Saved Resume (start from these, delta-edit)

| Target role archetype | Start from | Identity/summary angle |
|---|---|---|
| Backend / distributed systems | `Backend/` | reliability metrics forward |
| Full-stack / web | `FullStack/` | React/TypeScript + services |
| Data platform / data eng | `DataPlatform/` | pipelines, streaming, governance |
| Applied AI / LLM / agents | `AI/` | agentic + RAG + MCP |

(One row per archetype. `library.pick_archetype` scores a JD against the description +
angle text and picks the base résumé folder/file named in the middle column.)

## Discovered Experiences (do NOT re-interview; already confirmed by user)

1. **Example experience** — short description, scope, metrics, and the truthfulness
   boundary (what is and is NOT true about it).

## Truthfulness Boundaries (never exceed)

- List skills/claims the candidate does NOT have, so no résumé ever overstates them.

## Style Rules (apply to ALL output)

- One full, tight page. No em dashes. No inline bold inside skills lines. See the skill.

## Generation Pipeline (token-efficient; do NOT hand-author HTML/DOCX)

1. Author only the résumé `.md`. 2. Run `resumes/_assets/build_resume.py`. 3. Save.

## Core Metrics Bank (canonical numbers, never alter)

- The candidate's reusable, verified metrics (throughput, latency, scale, etc.).

## Summary Variants (reuse, adjust metrics emphasis)

- **AI**: "..." · **Data**: "..." · **Full-stack**: "..."

## Skills Category Blocks (reuse per archetype)

- **Category:** comma-separated skills (no inline bold on these lines).

## Application History

| Date | Company | Role | Coverage | Status |
|---|---|---|---|---|
| 2026-01-01 | Example Co | Software Engineer | ~85% | saved |
