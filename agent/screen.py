#!/usr/bin/env python3
"""
screen.py - deterministic eligibility / fit classifier for the resume agent.

Pure regex, no model, no network. Given a fetched job dict it returns one of
three verdicts so the orchestrator can act without spending tokens:

    HARD_DROP     an eligibility blocker or a dead posting. Never reaches the
                  model; the orchestrator records it dropped.
    NEEDS_REVIEW  a judgment call the user should make (entry-level + no comp,
                  comp under target, a central-language-only requirement, a
                  PERM filing). Parked for the human, not guessed.
    BUILD         clear enough to hand to the /resume-tailoring skill.

This encodes the same heuristics the interactive skill applies (see
resumes/_index/LIBRARY.md "JD Screening Heuristics" and "Truthfulness
Boundaries"). Nuanced calls that need the library or the JD's full requirement
matching (projected coverage < ~65%, subtle title-vs-body drift) are left to
the skill's own in-run judgment, which routes to needs_review too.

    from screen import classify
    verdict = classify({"title": ..., "location": ..., "text": ..., "removed": False})

`classify` returns a dict: {verdict, reason, comp, years, notes}.
"""

import re

# Languages the candidate does NOT have (user-confirmed in LIBRARY.md
# Truthfulness Boundaries / hard-skill absences). A requirement that gates
# solely on one of these is not closable by framing. Patterns use word
# boundaries so "go" does not match inside "ongoing"/"category".
ABSENT_LANGS = {
    "go": r"\b(go|golang)\b",
    "rust": r"\brust\b",
    "c++": r"c\+\+",
    "c#": r"c#",
    "objective-c": r"\bobjective-c\b",
    "scala": r"\bscala\b",
}
# Languages the candidate genuinely has, which soften an "or" language line.
PRESENT_LANGS = ["python", "java", "typescript", "javascript", "node", "sql"]

PAY_FLOOR = 100_000  # LIBRARY.md pay test: build a level/ceiling role if comp tops this.


# ---------------------------------------------------------------- compensation

def parse_comp(text):
    """Return (low, high) annual USD ints, or None. Tolerant of the formats
    seen across ATSs: '$182,800 - $247,300', '$116,000 — $174,000 USD',
    '$110,400.00 - 165,600.00', '$215-230K', flat '$112,800 - $112,800'."""
    t = text.replace("—", "-").replace("–", "-").replace(" ", " ")

    def money(tok):
        tok = tok.strip().lower().replace(",", "").replace("$", "").replace("usd", "").strip()
        if not tok:
            return None
        mult = 1
        if tok.endswith("k"):
            mult = 1_000
            tok = tok[:-1]
        try:
            val = float(tok) * mult
        except ValueError:
            return None
        # a bare "165600.00" style
        return int(val)

    # Range: two money tokens joined by - / to, at least one carrying $ or k.
    rng = re.compile(
        r"\$?\s*([\d][\d,]*(?:\.\d+)?\s*[kK]?)\s*(?:-|to|→)\s*\$?\s*([\d][\d,]*(?:\.\d+)?\s*[kK]?)"
    )
    for m in rng.finditer(t):
        lo, hi = money(m.group(1)), money(m.group(2))
        if not (lo and hi):
            continue
        # Normalise a K-scale mismatch where only one side carried 'K'
        # ('$215-230K' parses as 215 and 230000).
        if lo < 1000 <= hi:
            lo *= 1000
        if hi < 1000 <= lo:
            hi *= 1000
        if 20_000 <= lo <= 2_000_000 and hi >= lo:
            return (lo, hi)
    return None


# ----------------------------------------------------------------- eligibility

_EEO_NEAR = re.compile(
    r"(race|religio|national origin|protected|veteran|disabilit|gender|"
    r"sexual orientation|ancestry|marital|pregnan|genetic)",
    re.I,
)


def _sentences(text):
    return re.split(r"(?<=[.!?;])\s+|\n+", text)


def sponsorship_block(text):
    """A *statement* that sponsorship won't be provided (not the neutral
    application-form question 'will you require sponsorship?')."""
    neg = re.compile(
        r"\b(not|unable|cannot|can't|won't|will not|does not|do not|no|without|"
        r"neither|nor|ineligible|not able|not eligible|not available|not provide|"
        r"not offer|not sponsor)\b", re.I)
    for s in _sentences(text):
        if "sponsor" not in s.lower() and "visa" not in s.lower():
            continue
        low = s.lower().strip()
        # Skip the neutral form-field question.
        if low.endswith("?") or low.startswith(("will you", "do you", "are you", "would you")):
            continue
        if "sponsor" in low and neg.search(low):
            return s.strip()
    return None


def clearance_block(text):
    for s in _sentences(text):
        low = s.lower()
        if "clearance" not in low and "ts/sci" not in low and "top secret" not in low:
            continue
        if re.search(r"(required|must|active|obtain|maintain|eligible|ability to)", low):
            return s.strip()
    return None


def export_control_block(text):
    for s in _sentences(text):
        low = s.lower()
        hit = (
            "export control" in low
            or "export-control" in low
            or re.search(r"\bitar\b", low)
            or "u.s. person" in low
            or "us person" in low
        )
        if not hit:
            continue
        if _EEO_NEAR.search(s):  # protected-class boilerplate false positive
            continue
        return s.strip()
    return None


def citizenship_block(text):
    for s in _sentences(text):
        low = s.lower()
        if _EEO_NEAR.search(s):
            continue
        if re.search(r"(u\.s\.?\s+citizen|us citizen|citizens only|"
                     r"citizen or permanent resident|green card required|"
                     r"must be a citizen)", low) and re.search(r"(required|must|only|eligible)", low):
            return s.strip()
    return None


# -------------------------------------------------------------------- level

def years_ceiling(text):
    """Return the ceiling string if the JD caps experience (e.g. '1-2 years',
    '0-2 years'); None if it states a floor ('5+ years') or nothing."""
    for m in re.finditer(r"(\d)\s*[-–to]{1,3}\s*(\d)\+?\s*years", text, re.I):
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi <= 3 and lo <= hi:  # a genuine early-career cap
            return m.group(0).strip()
    return None


ENTRY_TITLE = re.compile(
    r"\b(junior|jr\.?|entry[- ]level|new grad(uate)?|intern|associate|"
    r"software engineer i\b|swe i\b|engineer i\b|early career)\b", re.I)


# -------------------------------------------------------------------- PERM

def perm_markers(text):
    hits = []
    if re.search(r"#LI-DNI\b", text):
        hits.append("#LI-DNI")
    low = text.lower()
    if "foreign equivalent" in low:
        corrob = [c for c in ("commuting distance", "multiple positions",
                              "two (2)", "(2) two", "in the job offered",
                              "special skill requirements",
                              "or as software engineer") if c in low]
        if corrob:
            hits.append("foreign-equivalent + " + corrob[0])
    if "[multiple positions" in low or "multiple positions available" in low:
        hits.append("multiple positions")
    return hits


# ---------------------------------------------------------- central-language gate

def language_gate(text):
    """Flag when a minimum qualification requires a language the candidate does
    not have, with no held language offered as an alternative near it. Kept
    conservative: only fires on an explicit required/proficiency phrasing that
    is not softened by 'or'/'and/or' with a held language."""
    low = text.lower()
    for lang, pat in ABSENT_LANGS.items():
        # find "must ... <lang>", "proficiency in <lang>", "strong <lang>",
        # "expert ... <lang>", "<lang> is required"
        for m in re.finditer(pat, low):
            a, b = max(0, m.start() - 70), min(len(low), m.end() + 70)
            window = low[a:b]
            required = re.search(
                r"(must|required|proficien|expert|strong|deep|solid|"
                r"hands-on|primary language|write .* in)", window)
            if not required:
                continue
            # softened if a held language appears in the same window with 'or'
            if (" or " in window or "and/or" in window or "or similar" in window) and \
               any(pl in window for pl in PRESENT_LANGS):
                continue
            # softened if a held language is required right alongside
            if any(pl in window for pl in PRESENT_LANGS):
                continue
            return f"required language '{lang}' with no held-language alternative"
    return None


# -------------------------------------------------------------------- classify

def classify(job):
    text = job.get("text") or ""
    title = job.get("title") or ""
    location = job.get("location") or ""
    notes = []

    if job.get("removed"):
        return {"verdict": "HARD_DROP", "reason": "posting removed or unretrievable",
                "comp": None, "years": None, "notes": notes}

    if not text or len(text) < 80:
        return {"verdict": "NEEDS_REVIEW", "reason": "JD text too short to screen (fetch likely failed)",
                "comp": None, "years": None, "notes": notes}

    # ---- HARD blockers (eligibility) ----
    for fn, label in (
        (sponsorship_block, "sponsorship not provided"),
        (clearance_block, "security clearance required"),
        (export_control_block, "export control / U.S. person required"),
        (citizenship_block, "citizenship required"),
    ):
        sentence = fn(text)
        if sentence:
            return {"verdict": "HARD_DROP", "reason": f"{label}: \"{sentence[:180]}\"",
                    "comp": None, "years": years_ceiling(text), "notes": notes}

    comp = parse_comp(text)
    years = years_ceiling(text)
    perm = perm_markers(text)
    lang = language_gate(text)

    onsite = bool(re.search(r"\bon[- ]?site\b|hybrid|in[- ]office|days? (a|per) week", text, re.I))
    if onsite and location and not re.search(r"new york|nyc|ny\b|remote", location, re.I):
        notes.append(f"onsite/hybrid in {location} (relocation)")

    # ---- NEEDS_REVIEW (judgment calls) ----
    if perm:
        return {"verdict": "NEEDS_REVIEW", "reason": "PERM markers: " + ", ".join(perm),
                "comp": comp, "years": years, "notes": notes}

    if lang:
        return {"verdict": "NEEDS_REVIEW", "reason": lang,
                "comp": comp, "years": years, "notes": notes}

    if comp and comp[1] < PAY_FLOOR:
        return {"verdict": "NEEDS_REVIEW",
                "reason": f"comp tops ${comp[1]:,}, below ${PAY_FLOOR:,} target",
                "comp": comp, "years": years, "notes": notes}

    entry = bool(ENTRY_TITLE.search(title)) or years is not None
    if entry and not comp:
        why = "entry/ceiling level" + (f" ({years})" if years else "") + ", no comp stated"
        return {"verdict": "NEEDS_REVIEW", "reason": why,
                "comp": comp, "years": years, "notes": notes}

    # ---- BUILD ----
    reason = "clear fit"
    if years and comp and comp[1] >= PAY_FLOOR:
        reason = f"years ceiling {years} but comp tops ${comp[1]:,} (pay test passes)"
    return {"verdict": "BUILD", "reason": reason, "comp": comp, "years": years, "notes": notes}


if __name__ == "__main__":
    # Tiny smoke test against this session's real decisions.
    cases = [
        ("New Relic", {"title": "Software Engineer-ELB", "location": "Portland, OR",
                       "text": "Great infra role. Please note that visa sponsorship is not "
                               "available for this position. 3+ years with Go or Python."}),
        ("ICON", {"title": "AI Software Engineer II", "location": "Remote",
                  "text": "Build LLM agents. Must be able to obtain and maintain security "
                          "clearance. 6+ years experience."}),
        ("Duolingo", {"title": "Senior Software Engineer, Backend", "location": "New York, NY",
                      "text": "Backend at scale. Experience in Java, Python, or Kotlin. "
                              "Salary Range: $182,800 - $247,300 USD. 5+ years."}),
        ("Pipe17", {"title": "Junior Software Engineer, AI-Native", "location": "Seattle, WA",
                    "text": "On-site. 1-2 years out of college. Competitive entry-level salary "
                            "and benefits. Node.js and TypeScript."}),
        ("Dragos-Rust", {"title": "Senior Backend Engineer, Rust", "location": "Remote",
                         "text": "We build distributed systems for OT security. Minimum "
                                 "qualifications: solid experience and understanding of Rust "
                                 "is required, and every responsibility is written in Rust. "
                                 "Kubernetes and Docker a plus. $165,000 plus equity."}),
        ("EEO-guard", {"title": "SWE", "location": "Remote",
                       "text": "We do not discriminate based on race, religion, citizenship, "
                               "national origin, or veteran status. $150,000 - $180,000. 5+ years."}),
    ]
    for name, job in cases:
        r = classify(job)
        print(f"{name:14} -> {r['verdict']:12} | {r['reason'][:70]}")
