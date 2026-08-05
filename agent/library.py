#!/usr/bin/env python3
"""
library.py - assemble the authoring context from LIBRARY.md so the headless skill
run never re-reads the whole 35K-token library or scans the resumes folder.

The context is split in two, because roughly 80% of it is the same for every
role in a queue:

  build_prime_context()  ~4.0k tok  candidate facts, discovered experiences,
                                    boundaries, style rules, metrics, summary
                                    variants, skills blocks. IDENTICAL for every
                                    role, so it is loaded ONCE into the primed
                                    base session and is a cache hit thereafter.

  build_job_kit()        ~2.6k tok  per role: the full archetype table in
                                    compact score-ordered form plus the ONE
                                    chosen base resume to delta-edit.

Previously both halves were concatenated and re-read per role (~9.8k tokens of
cache-creation every time); splitting them saves ~7.2k fresh tokens per resume.

Both deliberately drop the bulk that never helps author a single resume: the
~120-row Application History, company notes, JD screening heuristics, research
shortcuts.

    from library import build_prime_context, build_job_kit
    prime = build_prime_context()                       # once per queue
    kit   = build_job_kit(jd_text, company, title)      # once per role
"""

import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "resumes" / "_index" / "LIBRARY.md"
RESUMES = ROOT / "resumes"

# Sections that are IDENTICAL for every role. These go into the primed base
# session once, so every job reads them as a cheap cache hit instead of paying
# to re-create them (~8k tokens per role otherwise).
#
# "Role Archetypes" is deliberately NOT here: the table is 47 rows / ~4.2k
# tokens and a given role needs the handful that actually match, so it is
# shipped per-job as a scored shortlist instead (see build_job_kit).
KEEP = [
    "Candidate Facts", "Discovered Experiences",
    "Track Preferences", "Truthfulness Boundaries", "Style Rules",
    "Generation Pipeline", "Core Metrics Bank", "Summary Variants",
    "Skills Category Blocks",
]

# Archetype rows are shipped per-job, score-ordered, in COMPACT form: the `desc`
# and base path only, dropping the long `angle` column (authoring guidance that
# is only useful once a base is chosen). Measured: all 47 rows compacted is
# ~1.06k tokens vs ~4.23k for the full table, so the whole table fits for a
# quarter of the cost. Keeping every row matters because keyword scoring is
# wrong about a third of the time on the labeled set -- a truncated shortlist
# would send the author back to `ls resumes/` for exactly those cases, which is
# the 2-turn detour this is meant to remove. The angle is shown in full for the
# chosen row only.
HIGHLIGHT = 5   # rows called out as the best matches, ahead of the rest

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

# ATS chrome and legal/benefits boilerplate. Fetched JD text is mostly this: on
# the Reddit Ads posting the top body terms were nbsp(11) and information(10)
# while the actual signal, "ads", appeared 6 times. Scoring against the raw text
# ranks rows by how much benefits language they happen to share.
BOILER = set((
    "nbsp amp quot lt gt href span div class style font br li ul www http https com "
    "information benefits job jobs position level base personal day time may all any "
    "one more including most right apply application applicants employment employer "
    "opportunity equal candidates candidate compensation salary range offer total "
    "rewards paid leave insurance medical dental vision equity bonus stock location "
    "remote office hybrid onsite company companies about our us we're join looking "
    "qualifications responsibilities requirements preferred required nice must should "
    "please note visit learn read policy privacy accommodation disability veteran "
    "gender race religion age sexual orientation identity national origin protected "
    "status law laws regard without basis applicable federal state local city new york "
    "san francisco seattle austin boston chicago usd year annually").split())


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
    return {w for w in re.findall(r"[a-z0-9+#]+", s.lower())
            if len(w) > 2 and w not in STOP and w not in BOILER}


_CORPUS_CACHE = {}


def _idf(docs):
    """Inverse document frequency over a list of keyword sets."""
    n, df = len(docs), {}
    for d in docs:
        for w in d:
            df[w] = df.get(w, 0) + 1
    return {w: math.log(1 + n / c) for w, c in df.items()}


def _corpus():
    """Keyword sets + IDF for both scoring signals: the archetype table's own
    label text, and each base resume's full text. Cached; ~47 file reads, zero
    model tokens."""
    if _CORPUS_CACHE:
        return _CORPUS_CACHE
    labels, bases = {}, {}
    for desc, base, angle in archetype_rows():
        # desc ONLY. The `angle` column is authoring guidance for AFTER a base is
        # chosen ("The Omada base was missing Airflow and DBT entirely...") and
        # is full of generic words that match every JD, so including it ranked
        # rows by how verbose their notes were.
        labels[base] = _kw(desc)
        p = resolve_base(base)
        if p and p.exists():
            # The company folder name is signal too (a Snap ads resume for an ads JD).
            bases[base] = _kw(p.read_text(encoding="utf-8", errors="replace")
                              + " " + p.parent.name)
    _CORPUS_CACHE.update(
        labels=labels, bases=bases,
        label_idf=_idf(list(labels.values())), base_idf=_idf(list(bases.values())))
    return _CORPUS_CACHE


TITLE_WEIGHT = 6   # the role title is worth this many body mentions


def score_archetypes(jd_text, title=""):
    """Rank every archetype for this JD. Returns [(total, desc, base, angle), ...]
    best first.

    The old scoring was |jd_keywords & label_keywords| - a raw set-intersection
    count over the archetype table's own description+angle text. Two flaws made
    it mis-pick: it rewarded verbose labels (more words, more chances to
    intersect), and it treated every matched term as equally informative, so a
    JD about "ads" scored "ads" no higher than "engineering". On the Reddit Ads
    JD the correct row scored 4 while a generic infrastructure row scored 14,
    and the author spent two turns listing the folder and reading a different
    resume to correct it.

    So: TF-IDF instead of set overlap. A term is weighted by how often the JD
    uses it (a JD that says "ads" fifteen times means it) times how rare it is
    across the corpus (terms in every row - "engineering", "platform" - carry no
    discriminative signal; "auction", "bidding", "ads" do). Scores are length-
    normalized so a wordy row cannot win on verbosity alone. The base resume's
    own text is scored the same way as a secondary signal.
    """
    c = _corpus()
    # The title ("Software Engineer, Ads") is the densest signal in the whole
    # posting, so it is repeated before the body rather than diluted into it.
    text = ((title or "") + " ") * TITLE_WEIGHT + jd_text
    jd_tf = {}
    for w in re.findall(r"[a-z0-9+#]+", text.lower()):
        if len(w) > 2 and w not in STOP and w not in BOILER:
            jd_tf[w] = jd_tf.get(w, 0) + 1
    jd_tf = {w: 1 + math.log(n) for w, n in jd_tf.items()}   # damp raw counts

    def sim(kws, idf):
        if not kws:
            return 0.0
        hit = sum(jd_tf[w] * idf.get(w, 0.0) for w in kws if w in jd_tf)
        return hit / math.sqrt(len(kws))   # length-normalize

    out = []
    for desc, base, angle in archetype_rows():
        label = sim(c["labels"].get(base, set()), c["label_idf"])
        resume = sim(c["bases"].get(base, set()), c["base_idf"])
        # The curated label is the primary signal; the resume text breaks ties
        # and catches rows whose label wording undersells the match.
        out.append((label + 0.4 * resume, desc, base, angle))
    out.sort(key=lambda r: -r[0])
    return out


def pick_archetype(jd_text, title=""):
    """Best-scoring archetype. Returns ((desc, base_path, angle), confidence)."""
    ranked = score_archetypes(jd_text, title)
    if not ranked:
        return None, -1
    total, desc, base, angle = ranked[0]
    return (desc, base, angle), total


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


CONF_FLOOR = 6.0   # score above which the archetype match is considered clear


def build_prime_context():
    """The role-INVARIANT half of the authoring kit.

    Loaded once into the primed base session so every job in the queue reads it
    as a cache hit rather than re-creating ~8k tokens per role.
    """
    return "\n".join([
        "# CANDIDATE AUTHORING CONTEXT (applies to every resume in this queue)",
        "These facts, boundaries and style rules govern EVERY resume you author in "
        "this session. Hold them; you will get one small per-role kit per job. Do NOT "
        "open resumes/_index/LIBRARY.md and do NOT scan the resumes/ folder.",
        "",
        curated_slices(),
    ])


def build_job_kit(jd_text, company, title=""):
    """The role-SPECIFIC half: the chosen base resume plus the full archetype
    table in compact score-ordered form, so switching bases costs no file reads."""
    ranked = score_archetypes(jd_text, title)
    if not ranked:
        return {"kit_text": "", "base_rel": None, "archetype": None,
                "confidence": -1, "research": True, "runner_up": None,
                "runner_up_score": None}
    conf, adesc, abase, aangle = ranked[0]
    base = resolve_base(abase)
    base_rel = str(base.relative_to(ROOT)) if base else None

    # Research gate: skip only when we're confident AND the company is familiar.
    research = not (familiar_company(company) and conf >= CONF_FLOOR)

    table = ["# ARCHETYPE TABLE (every base, ordered by fit against THIS JD)",
             "The top rows scored best, but the score is keyword-based and is wrong "
             "roughly a third of the time - trust the JD over the ordering. To switch, "
             "read ONLY the one file you pick; do NOT list or scan resumes/."]
    for i, (total, desc, b, _angle) in enumerate(ranked):
        if i == HIGHLIGHT:
            table.append("  --- remaining archetypes ---")
        table.append(f"- [{total:5.2f}] {desc}  ->  `{b}`")

    parts = [
        "# PER-ROLE KIT",
        "Start from the CHOSEN BASE RESUME below and delta-edit it. Everything else "
        "you need is already loaded in this session.",
        "",
        "\n".join(table),
    ]
    if base and base.exists():
        parts += [
            "\n---\n",
            f"# CHOSEN BASE RESUME  ({base_rel})",
            f"# archetype: {adesc}  ·  match score: {conf:.2f}",
            f"# how this archetype is usually framed: {aangle}",
            "Delta-edit THIS into the tailored resume for the role.\n",
            base.read_text(encoding="utf-8"),
        ]
    return {
        "kit_text": "\n".join(parts),
        "base_rel": base_rel,
        "archetype": adesc,
        "confidence": conf,
        "research": research,
        "runner_up": ranked[1][1] if len(ranked) > 1 else None,
        "runner_up_score": ranked[1][0] if len(ranked) > 1 else None,
    }


if __name__ == "__main__":
    import sys
    jd = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else "backend distributed systems kafka python aws kubernetes reliability"
    company = sys.argv[2] if len(sys.argv) > 2 else "acme"
    k = build_job_kit(jd, company, sys.argv[3] if len(sys.argv) > 3 else "")
    prime = build_prime_context()
    print("archetype :", k["archetype"])
    print("base      :", k["base_rel"])
    print("score     : %.1f  (runner-up %.1f: %s)"
          % (k["confidence"], k["runner_up_score"] or 0, k["runner_up"]))
    print("research  :", k["research"])
    print("prime ctx : %d chars (~%d tok)  [once per queue]" % (len(prime), len(prime) // 4))
    print("job kit   : %d chars (~%d tok)  [per role]" % (len(k["kit_text"]), len(k["kit_text"]) // 4))
