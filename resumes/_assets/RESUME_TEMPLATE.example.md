# Your Full Name
[email](mailto:you@example.com) | [linkedin](https://linkedin.com/in/you) | [github](https://github.com/you) | City, ST

## Summary
One tight paragraph (2–3 sentences). Lead with your archetype and strongest, **verifiable**
metrics. Use `**bold**` sparingly for the numbers that matter. No em dashes.

## Experience
### Company Name
**Job Title** | Location (or Remote) | Mon YYYY - Present
*Primary stack for this role, comma-separated (rendered in italics)*
- Achievement bullet: action + scope + **measurable result** (e.g. "cut p99 latency 40%").
- Keep bullets to one line each; lead with the verb; quantify wherever it's true.
- 3–5 bullets for a recent role, fewer for older ones.

### Previous Company
**Earlier Title** | Location | Mon YYYY - Mon YYYY
*Stack used here*
- Another quantified achievement.
- A second one showing a different competency.

## Skills
**Languages:** Python, Go, TypeScript, SQL
**Infrastructure:** AWS, Kubernetes, Terraform, Kafka
**Practices:** distributed systems, CI/CD, observability
<!-- No inline **bold** inside the items list — only the "**Category:**" label is bold. -->

## Projects
### Project Name [link](https://github.com/you/project)
*Tech stack (italic)*
- What it does and the impact / scale, in one line.
- A second bullet if it earns its place on a one-page résumé.

## Education
**University Name** | Location | Mon YYYY - Mon YYYY
B.S. in Computer Science. GPA (optional). Honors / relevant coursework (optional).

<!--
FORMAT NOTES (parsed by resumes/_assets/build_resume.py — keep the structure exact):
  • First "# " line  = your name (centered heading).
  • Next non-blank line = contact line, parts split on " | ", links as [text](url).
  • "## " = section. Experience / Skills / Projects / Education are recognized by name;
    any other "## Foo" renders as a plain paragraph section.
  • Under Experience/Projects: "### Heading", then an optional "**Title** | Loc | Dates"
    subline, an optional "*tech*" italic line, then "- " bullets.
  • Under Skills: one "**Category:** items" line each.
  • Build it:  python3 resumes/_assets/build_resume.py resumes/<Folder>/<file>.md
    → produces the .pdf (auto-fit to one page) and .docx next to the .md.
  • Keep it to ONE tight, full page. No em dashes. Copy this file to start a base résumé.
-->
