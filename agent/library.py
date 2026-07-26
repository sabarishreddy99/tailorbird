#!/usr/bin/env python3
"""
library.py - assemble a compact per-role "kit" from LIBRARY.md so the headless
skill run takes the skill's own token-efficient path without re-reading the
whole 35K-token library or scanning the resumes folder.

The kit contains ONLY the authoring-relevant sections (candidate facts,
archetype table, discovered experiences, boundaries, style rules, generation
pipeline, metrics, summary variants, skills blocks) plus the ONE nearest base
resume. It deliberately drops the bulk that never helps author a single resume:
the ~120-row Application History, company-specific notes, JD screening
heuristics, and research shortcuts.

    from library import build_kit
    kit = build_kit(jd_text, company)   # dict: kit_text, base_rel, archetype, confidence, research
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "resumes" / "_index" / "LIBRARY.md"
RESUMES = ROOT / "resumes"

# Sections to keep in the kit (prefix match against the "## " heading text).
KEEP = [
    "Candidate Facts", "Role Archetypes", "Discovered Experiences",
    "Track Preferences", "Truthfulness Boundaries", "Style Rules",
    "Generation Pipeline", "Core Metrics Bank", "Summary Variants",
    "Skills Category Blocks",
]

# Household-name employers whose generic background needs no web research (the JD
# is already supplied). Keys are normalized: lowercase, non-alphanumerics stripped
# (so "T-Mobile" -> "tmobile"), matching familiar_company()'s normalization.
KNOWN_COMPANIES = {
    # Big tech / infra
    "google", "apple", "amazon", "microsoft", "meta", "netflix", "stripe",
    "datadog", "figma", "duolingo", "snap", "uber", "airbnb", "salesforce",
    "nvidia", "openai", "anthropic", "linkedin", "adobe", "oracle", "ibm",
    "coinbase", "databricks", "snowflake", "atlassian", "shopify", "block",
    "paypal", "cloudflare", "pinterest", "reddit", "roblox", "roku", "twilio",
    "spotify", "dropbox", "lyft", "doordash", "instacart", "robinhood", "plaid",
    "samsung", "sony", "dell", "hp", "vmware", "redhat", "sap", "servicenow",
    "workday", "intuit", "qualcomm", "amd", "tesla", "palantir", "zoom", "slack",
    "docusign", "okta", "crowdstrike", "paloaltonetworks", "mongodb", "elastic",
    "hashicorp", "gitlab", "github", "confluent", "ebay", "twitter", "x",
    # Telecom / carriers
    "tmobile", "verizon", "att", "comcast", "charter", "spectrum",
    # Banks / finance / payments
    "jpmorgan", "jpmorganchase", "chase", "bankofamerica", "wellsfargo", "citi",
    "citibank", "citigroup", "capitalone", "goldmansachs", "morganstanley",
    "americanexpress", "amex", "visa", "mastercard", "fidelity", "charlesschwab",
    "usbank", "pnc", "discover", "blackrock",
    # Retail / consumer / logistics / health
    "walmart", "target", "costco", "homedepot", "lowes", "nike", "starbucks",
    "disney", "fedex", "ups", "cvs", "walgreens", "unitedhealth", "cigna",
    "kaiserpermanente", "att",
}

STOP = set(("the a an and or of to in for with on at by from as is are be this that "
            "you your we our they will can role team work years experience engineer "
            "software strong plus etc using build built help across into their than "
            "who what when where which while have has had not but its it's").split())


def _sections():
    """Return {heading_text: full_block_with_heading}."""
    text = LIBRARY.read_text(encoding="utf-8")
    out = {}
    for chunk in re.split(r"(?m)^## ", text)[1:]:
        title = chunk.splitlines()[0].strip()
        out[title] = "## " + chunk.rstrip() + "\n"
    return out


def curated_slices():
    secs = _sections()
    keep = [b for t, b in secs.items() if any(t.startswith(k) for k in KEEP)]
    return "\n".join(keep)


def archetype_rows():
    secs = _sections()
    body = next((b for t, b in secs.items() if t.startswith("Role Archetypes")), "")
    rows = []
    for line in body.splitlines():
        m = re.match(r"\|\s*(.+?)\s*\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|", line)
        if m and "Target role archetype" not in m.group(1):
            rows.append((m.group(1), m.group(2), m.group(3)))
    return rows


def resolve_base(base_path):
    """Turn an archetype base like 'Apple/', 'Snap/...Full_Stack_L4', or
    'Name_..._Resume_Backend.md' into a concrete resume .md path."""
    bp = base_path.strip()
    # Company folder -> first non-report .md
    folder = RESUMES / bp.rstrip("/")
    if bp.endswith("/") or folder.is_dir():
        if folder.is_dir():
            mds = sorted(p for p in folder.glob("*.md") if "Report" not in p.name)
            if mds:
                return mds[0]
    # Company/partial-filename
    if "/" in bp:
        comp, partial = bp.split("/", 1)
        f = RESUMES / comp
        partial = partial.replace("...", "").strip("_ ")
        if f.is_dir():
            for p in f.glob("*.md"):
                if "Report" in p.name:
                    continue
                if partial and partial.lower() in p.name.lower():
                    return p
            mds = [p for p in f.glob("*.md") if "Report" not in p.name]
            if mds:
                return mds[0]
    # Root file with an ellipsis, e.g. Name_..._Resume_Backend.md
    if bp.endswith(".md"):
        pat = "*" + re.sub(r"^.*\.\.\._?", "", bp)
        for p in RESUMES.glob(pat):
            if p.is_file():
                return p
    return None


def _kw(s):
    return {w for w in re.findall(r"[a-z0-9+#]+", s.lower()) if len(w) > 2 and w not in STOP}


def pick_archetype(jd_text):
    """Score the JD against each archetype's description+angle. Returns
    ((desc, base_path, angle), confidence)."""
    jd = _kw(jd_text)
    best, best_score = None, -1
    for desc, base, angle in archetype_rows():
        score = len(jd & _kw(desc + " " + angle))
        if score > best_score:
            best, best_score = (desc, base, angle), score
    return best, best_score


def familiar_company(company):
    norm = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    if not norm:
        return False
    if norm in KNOWN_COMPANIES:
        return True
    for d in RESUMES.iterdir():
        if d.is_dir() and re.sub(r"[^a-z0-9]", "", d.name.lower()) == norm:
            return True
    return False


def build_kit(jd_text, company):
    arch, conf = pick_archetype(jd_text)
    base_path = arch[1] if arch else None
    base = resolve_base(base_path) if base_path else None
    base_rel = str(base.relative_to(ROOT)) if base else None

    # Research gate: skip only when we're confident AND the company is familiar.
    research = not (familiar_company(company) and conf >= 4)

    parts = [
        "# AUTHORING KIT",
        "This kit is everything you need to tailor this ONE resume. Do NOT open "
        "resumes/_index/LIBRARY.md and do NOT scan the resumes/ folder; it wastes tokens. "
        "Start from the CHOSEN BASE RESUME below and delta-edit it. You MAY switch to a "
        "different base from the Role Archetypes table if this JD clearly fits another.",
        "",
        curated_slices(),
    ]
    if base and base.exists():
        parts += [
            "\n---\n",
            f"# CHOSEN BASE RESUME  ({base_rel})",
            f"# archetype: {arch[0]}  ·  match-confidence: {conf}",
            "Delta-edit THIS into the tailored resume for the role.\n",
            base.read_text(encoding="utf-8"),
        ]
    return {
        "kit_text": "\n".join(parts),
        "base_rel": base_rel,
        "archetype": arch[0] if arch else None,
        "confidence": conf,
        "research": research,
    }


if __name__ == "__main__":
    import sys
    jd = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else "backend distributed systems kafka python aws kubernetes reliability"
    company = sys.argv[2] if len(sys.argv) > 2 else "acme"
    k = build_kit(jd, company)
    print("archetype :", k["archetype"])
    print("base      :", k["base_rel"])
    print("confidence:", k["confidence"])
    print("research  :", k["research"])
    print("kit chars :", len(k["kit_text"]), "(~%d tok)" % (len(k["kit_text"]) // 4))
