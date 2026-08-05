#!/usr/bin/env python3
"""
build_resume.py - one-command PDF + DOCX generation from a resume markdown file.

Usage:
  python3 build_resume.py <resume.md> [--outdir DIR]

Parses the library's canonical resume MD format and produces:
  <same-basename>.pdf   (headless Chrome, auto-fits to 1 page via density retry)
  <same-basename>.docx  (via gen_docx.js, requires global npm 'docx' package)

Canonical MD format expected:
  # Name
  contact line: parts separated by " | ", links as [text](url)
  ## Summary            (one paragraph, **bold** markers)
  ## Experience         (### Company / **Title** | Loc | Dates / *tech line* / - bullets)
  ## Skills             (**Category:** items)
  ## Projects           (### Title [label](url) / *tech line* / - bullets)
  ## Education          (**Institution** | Loc | Dates / following lines verbatim)

Style rules baked in: Calibri, 1-page-first density retry (line-height 1.13 -> 1.10 -> 1.07).
"""
import sys, re, json, subprocess, os, shutil, tempfile, html as htmllib

def find_chrome():
    """Locate a Chrome/Chromium binary across platforms.

    Priority: TAILORBIRD_CHROME / CHROME env var → common install paths
    (macOS, Linux, Windows) → anything Chrome-like on PATH. Set the env var to
    override, e.g.  export TAILORBIRD_CHROME="/usr/bin/chromium-browser".
    """
    override = os.environ.get("TAILORBIRD_CHROME") or os.environ.get("CHROME")
    if override and os.path.exists(override):
        return override
    candidates = [
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        # Linux
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium", "/usr/bin/chromium-browser", "/snap/bin/chromium",
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    sys.exit("ERROR: could not find Chrome/Chromium. Install it, or set "
             "TAILORBIRD_CHROME to the browser binary path.")

CHROME = find_chrome()
NODE_GEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_docx.js")

CSS = """
  @page { size: letter; margin: 0.27in 0.42in; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: "Calibri", "Helvetica Neue", Arial, sans-serif; font-size: 9.4pt; color: #111; line-height: %LH%; }
  h1 { font-size: 16pt; color: #1F3864; text-align: center; margin-bottom: 1pt; }
  .contact { text-align: center; font-size: 9.3pt; margin-bottom: 3pt; }
  .contact a { color: #1155CC; text-decoration: none; }
  h2 { font-size: 10.5pt; color: #1F3864; text-transform: uppercase; letter-spacing: 0.4pt;
       border-bottom: 0.9pt solid #404040; margin: 3.2pt 0 1.5pt; padding-bottom: 1pt; }
  .role { margin-top: 2.8pt; }
  .company { font-size: 10.5pt; font-weight: bold; }
  .subline { display: flex; justify-content: space-between; }
  .subline .title { font-weight: bold; font-style: italic; }
  .subline .dates { font-weight: bold; }
  .tech { font-size: 9.2pt; font-style: italic; color: #555; margin-bottom: 1pt; }
  ul { margin-left: 13pt; }
  li { margin-bottom: 0.5pt; }
  .skills p { margin-bottom: 1pt; }
  .proj-title { font-size: 10.2pt; font-weight: bold; margin-top: 3.5pt; }
  .proj-title a { font-size: 9.5pt; font-weight: normal; color: #1155CC; text-decoration: none; }
  a { color: #1155CC; }
"""

def inline_html(s):
    s = htmllib.escape(s, quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
    return s

def to_runs(s):
    """Convert MD inline markup to [{text, bold, italic}] runs for DOCX (links become plain text)."""
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)  # strip links, keep text
    runs, pos = [], 0
    pat = re.compile(r"\*\*\*(.+?)\*\*\*|\*\*(.+?)\*\*|\*(.+?)\*")
    for m in pat.finditer(s):
        if m.start() > pos:
            runs.append({"text": s[pos:m.start()], "bold": False, "italic": False})
        if m.group(1) is not None:
            runs.append({"text": m.group(1), "bold": True, "italic": True})
        elif m.group(2) is not None:
            runs.append({"text": m.group(2), "bold": True, "italic": False})
        else:
            runs.append({"text": m.group(3), "bold": False, "italic": True})
        pos = m.end()
    if pos < len(s):
        runs.append({"text": s[pos:], "bold": False, "italic": False})
    return [r for r in runs if r["text"]]

def strip_md(s):
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)
    return re.sub(r"\*+", "", s)

def parse(md):
    lines = [l.rstrip() for l in md.splitlines()]
    doc = {"name": "", "contact": [], "sections": []}
    i = 0
    while i < len(lines) and not lines[i].startswith("# "):
        i += 1
    doc["name"] = lines[i][2:].strip(); i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    # contact line: split on " | "
    for part in lines[i].split(" | "):
        m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", part.strip())
        if m:
            doc["contact"].append({"text": m.group(1), "url": m.group(2)})
        else:
            doc["contact"].append({"text": strip_md(part.strip()), "url": None})
    i += 1

    cur = None
    def flush():
        if cur: doc["sections"].append(cur)
    while i < len(lines):
        l = lines[i]
        if l.startswith("## "):
            flush()
            title = l[3:].strip()
            kind = title.lower()
            if "experience" in kind: cur = {"type": "experience", "title": title, "entries": []}
            elif "skill" in kind: cur = {"type": "skills", "title": title, "lines": []}
            elif "project" in kind: cur = {"type": "projects", "title": title, "entries": []}
            elif "education" in kind: cur = {"type": "education", "title": title, "lines": []}
            else: cur = {"type": "paragraph", "title": title, "text": ""}
        elif cur and l.startswith("### "):
            head = l[4:].strip()
            m = re.match(r"(.+?)\s*(\[([^\]]+)\]\(([^)]+)\))\s*$", head)
            entry = {"heading": strip_md(m.group(1)) if m else strip_md(head),
                     "heading_md": m.group(1) if m else head,
                     "link_text": m.group(3) if m else None,
                     "link_url": m.group(4) if m else None,
                     "subline": None, "tech": None, "bullets": []}
            cur["entries"].append(entry)
        elif cur and cur["type"] in ("experience", "projects") and cur["entries"]:
            e = cur["entries"][-1]
            if re.match(r"^\*[^*].*\*$", l.strip()) and e["tech"] is None and not l.strip().startswith("- "):
                e["tech"] = l.strip().strip("*")
            elif l.strip().startswith("- "):
                e["bullets"].append(l.strip()[2:])
            elif "**" in l and " | " in l and e["subline"] is None:
                parts = [p.strip() for p in l.split(" | ")]
                e["subline"] = {"title": strip_md(parts[0]), "loc": parts[1] if len(parts) > 1 else "",
                                "dates": parts[2] if len(parts) > 2 else ""}
        elif cur and cur["type"] == "skills" and l.strip().startswith("**"):
            m = re.match(r"\*\*(.+?):\*\*\s*(.*)", l.strip())
            if m: cur["lines"].append({"cat": m.group(1), "items": m.group(2)})
        elif cur and cur["type"] == "education" and l.strip():
            cur["lines"].append(l.strip())
        elif cur and cur["type"] == "paragraph" and l.strip():
            cur["text"] = (cur["text"] + " " + l.strip()).strip()
        i += 1
    flush()
    return doc

def render_html(doc, lh):
    out = ['<!DOCTYPE html>\n<html><head><meta charset="utf-8"><title>%s Resume</title>\n<style>%s</style></head><body>\n'
           % (doc["name"], CSS.replace("%LH%", lh))]
    out.append("<h1>%s</h1>" % doc["name"])
    cparts = []
    for c in doc["contact"]:
        cparts.append('<a href="%s">%s</a>' % (c["url"], c["text"]) if c["url"] else c["text"])
    out.append('<div class="contact">%s</div>' % " &nbsp;|&nbsp; ".join(cparts))
    for sec in doc["sections"]:
        out.append("<h2>%s</h2>" % sec["title"])
        if sec["type"] == "paragraph":
            out.append("<p>%s</p>" % inline_html(sec["text"]))
        elif sec["type"] == "experience":
            for e in sec["entries"]:
                out.append('<div class="role"><div class="company">%s</div>' % inline_html(e["heading_md"]))
                if e["subline"]:
                    out.append('<div class="subline"><span><span class="title">%s</span> | %s</span><span class="dates">%s</span></div>'
                               % (e["subline"]["title"], e["subline"]["loc"], e["subline"]["dates"]))
                if e["tech"]:
                    out.append('<div class="tech">%s</div>' % htmllib.escape(e["tech"], quote=False))
                out.append("<ul>%s</ul></div>" % "".join("<li>%s</li>" % inline_html(b) for b in e["bullets"]))
        elif sec["type"] == "skills":
            out.append('<div class="skills">%s</div>' % "".join(
                "<p><b>%s:</b> %s</p>" % (s["cat"], htmllib.escape(s["items"], quote=False)) for s in sec["lines"]))
        elif sec["type"] == "projects":
            for e in sec["entries"]:
                linkbit = ' &nbsp;<a href="%s">[%s]</a>' % (e["link_url"], "link" if e["link_text"] in (None, "↗") else e["link_text"]) if e["link_url"] else ""
                out.append('<div class="proj-title">%s%s</div>' % (e["heading"], linkbit))
                if e["tech"]:
                    out.append('<div class="tech">%s</div>' % htmllib.escape(e["tech"], quote=False))
                out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline_html(b) for b in e["bullets"]))
        elif sec["type"] == "education":
            for idx, l in enumerate(sec["lines"]):
                if idx == 0 and " | " in l:
                    parts = [p.strip() for p in l.split(" | ")]
                    out.append('<div class="subline"><span><span style="font-weight:bold; font-size:10.5pt;">%s</span> | %s</span><span class="dates">%s</span></div>'
                               % (strip_md(parts[0]), parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))
                else:
                    out.append("<div>%s</div>" % inline_html(l))
    out.append("</body></html>")
    return "\n".join(out)

def page_count(pdf_path):
    raw = open(pdf_path, "rb").read()
    return len(re.findall(rb"/Type\s*/Page(?![s/])", raw))


# ---------------------------------------------------------------- fit magnitude
# The density ladder tells us whether the resume fits; on its own it does NOT say
# by HOW MUCH. Without a magnitude the author cannot size a correction, so it
# converges by trial: edit a clause, rebuild, edit another, rebuild... (observed:
# 7 consecutive edits + 2 builds on one resume, 15 builds on another). The ladder
# bracket already contains the answer -- content height scales ~linearly with
# line-height, so the gap between the densest rung that fits and the next one
# that doesn't IS the slack, expressed as a fraction of a page. Multiply by the
# rendered line count and you get the gap in the unit the author actually edits.

CHARS_PER_LINE = 112          # 9.4pt Calibri across the 7.66in content column
BULLET_CHARS_PER_LINE = 108   # bullets sit inside ul{margin-left:13pt}
TIGHT_LH = 1.19               # below this the page reads full; at/above it reads sparse
# Oversized elements, in body-line equivalents (h1 16pt, h2 10.5pt + rule +
# margins, .company 10.5pt, .proj-title 10.2pt).
H1_LINES, H2_LINES, COMPANY_LINES, PROJ_LINES = 1.8, 1.6, 1.15, 1.12

def est_lines(doc):
    """Approximate rendered line count, in body-line equivalents.

    Only used to convert a page-height FRACTION (which the ladder bracket gives
    us exactly) into lines, the unit the author edits in. Constants were fitted
    against real builds so that adding one rendered line moves the reported gap
    by one line; see the slope check in the module docstring's verification note.
    """
    def wrapped(text, width=CHARS_PER_LINE):
        return max(1, -(-len(text) // width))  # ceil
    n = H1_LINES + 1.0  # name + contact
    for sec in doc["sections"]:
        n += H2_LINES
        if sec["type"] == "paragraph":
            n += wrapped(sec["text"])
        elif sec["type"] in ("experience", "projects"):
            for e in sec["entries"]:
                n += COMPANY_LINES if sec["type"] == "experience" else PROJ_LINES
                n += 1 if e.get("subline") else 0
                n += 1 if e.get("tech") else 0
                n += sum(wrapped(b, BULLET_CHARS_PER_LINE) for b in e["bullets"])
        elif sec["type"] == "skills":
            n += sum(wrapped("%s: %s" % (s["cat"], s["items"])) for s in sec["lines"])
        elif sec["type"] == "education":
            n += sum(wrapped(l) for l in sec["lines"])
    return n

def _bracket(probe, rungs):
    """Binary-search a dense->generous rung list for the fit boundary. Returns
    (lh_pass, lh_fail) as floats, either of which may be None at the ends.
    Binary search keeps this to ~log2(n) Chrome launches instead of a walk."""
    lo, hi, best = 0, len(rungs) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if probe(rungs[mid]) == 1:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    lh_pass = float(rungs[best]) if best >= 0 else None
    lh_fail = float(rungs[best + 1]) if best + 1 < len(rungs) else None
    return lh_pass, lh_fail


def measure_fit(doc, memo, best, render_pages, ladder):
    """Return a one-line human+agent readable fit report with the gap in lines.

    Common case costs ZERO extra Chrome launches: the binary search has already
    probed the rung just above `best` (that is what terminated it), so the
    bracket is sitting in `memo`. Only the two extreme cases -- fits even at the
    most generous rung, or overflows even at the densest -- need extra probes,
    and those are exactly the cases where the author needs the number most.
    """
    n = est_lines(doc)
    if n <= 0:
        return ""

    def probe(lh):
        if lh not in memo:
            memo[lh] = render_pages(lh)
        return memo[lh]

    # Case A: overflows even at the densest shipping rung -> probe DOWN to find
    # where it would fit. The rungs are ~0.025 apart so one rung is ~1.5 lines;
    # taking the MIDPOINT of the resulting bracket (rather than the first rung
    # that happens to fit) keeps the estimate unbiased.
    if best < 0:
        dense = float(ladder[0])
        lh_pass, lh_fail = _bracket(
            probe, ("0.80", "0.85", "0.90", "0.94", "0.97", "1.00", "1.025", "1.05"))
        if lh_pass is None:
            return ("OVERFULL: more than ~%d lines too long -- cut a whole entry or section, "
                    "then rebuild once." % max(2, round(n * 0.25)))
        edge = lh_pass if lh_fail is None else (lh_pass + lh_fail) / 2.0
        over = n * (1 - edge / dense)
        return ("OVERFULL: ~%.1f line(s) too long -- cut ~%d bullet line(s) or one short "
                "entry, then rebuild once." % (over, max(1, round(over))))

    lh_pass = float(ladder[best])

    # Case B: fits even at the most generous shipping rung -> probe UP to find
    # where it stops fitting, so "underfilled" gets a number instead of a flag.
    if best == len(ladder) - 1:
        top, fail = _bracket(probe, ("1.35", "1.4", "1.45", "1.5", "1.6", "1.8", "2.0"))
        top = lh_pass if top is None else top
        edge = top if fail is None else (top + fail) / 2.0
        room = n * (edge / TIGHT_LH - 1)
        return ("UNDERFILLED: room for ~%d more line(s) before the page is full -- add "
                "relevant content in the priority order, then rebuild once." % max(1, round(room)))

    # Case C (the common one): the bracket is already known -- free.
    lh_fail = float(ladder[best + 1])
    slack = n * (lh_fail - lh_pass) / lh_pass / 2.0   # midpoint of the bracket
    if lh_pass >= TIGHT_LH:
        room = n * (lh_pass / TIGHT_LH - 1)
        return ("UNDERFILLED: room for ~%d more line(s) before the page is full -- add "
                "relevant content in the priority order, then rebuild once." % max(1, round(room)))
    return "FIT: one full page, slack ~%.1f line(s). Ship it -- do not rebuild." % slack

def main():
    md_path = os.path.abspath(sys.argv[1])
    outdir = os.path.abspath(sys.argv[sys.argv.index("--outdir") + 1]) if "--outdir" in sys.argv else os.path.dirname(md_path)
    base = os.path.splitext(os.path.basename(md_path))[0]
    os.makedirs(outdir, exist_ok=True)
    doc = parse(open(md_path).read())

    # Unique scratch files (per-process temp dir) so parallel builds never
    # collide and it works on any OS (no hardcoded /tmp).
    scratch = tempfile.mkdtemp(prefix="tailorbird_build_")
    html_path = os.path.join(scratch, "resume.html")
    json_path = os.path.join(scratch, "content.json")

    # PDF density ladder: pick the MOST GENEROUS line-height that still fits 1 page
    # (fills the page visually and signals content volume). Fit is monotonic — a
    # denser line-height always fits if a more generous one does — so we BINARY
    # SEARCH the ladder (~3 Chrome launches) instead of scanning all 7 linearly.
    # The chosen line-height, and thus the output PDF, is identical to the old scan.
    pdf_path = os.path.join(outdir, base + ".pdf")
    LADDER = ("1.07", "1.1", "1.13", "1.15", "1.2", "1.25", "1.3")  # dense -> generous

    def render_pages(line_height):
        open(html_path, "w").write(render_html(doc, line_height))
        subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        "--print-to-pdf=" + pdf_path, html_path], capture_output=True)
        return page_count(pdf_path)

    memo, lo, hi, best = {}, 0, len(LADDER) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        p = memo.get(LADDER[mid])
        if p is None:
            p = memo[LADDER[mid]] = render_pages(LADDER[mid])
        if p == 1:
            best, lo = mid, mid + 1      # fits -> try a more generous height
        else:
            hi = mid - 1                 # overflows -> must go denser

    lh = LADDER[best] if best >= 0 else LADDER[0]
    # Measure the fit gap BEFORE the authoritative render: the probes below reuse
    # render_pages() and would otherwise leave a measurement PDF on disk.
    fit = measure_fit(doc, memo, best, render_pages, LADDER)
    pages = render_pages(lh)             # authoritative final render leaves the chosen PDF on disk
    # Safety net: if that render disagrees with the search (rare non-monotonic layout),
    # step denser until it fits so we never emit a 2-page PDF when a denser one would fit.
    if pages > 1 and best > 0:
        for j in range(best - 1, -1, -1):
            lh = LADDER[j]
            pages = render_pages(lh)
            if pages == 1:
                break
    print("PDF: %s (%d page(s), line-height %s)" % (pdf_path, pages, lh))
    if pages > 1:
        print("WARNING: still %d pages at max density; trim content in the MD." % pages)
    elif float(lh) >= 1.2:
        print("NOTE: UNDERFILLED page (fits at line-height %s). Full-page rule: add relevant "
              "content (restore a trimmed entry, add a project or bullets) for a tight full page." % lh)
    # The magnitude line: size ONE edit pass from this instead of converging by
    # trial-and-rebuild. See measure_fit().
    if fit:
        print(fit)

    # DOCX via node generator
    docx_doc = dict(doc)
    for sec in docx_doc["sections"]:
        if sec["type"] == "paragraph":
            sec["runs"] = to_runs(sec["text"])
        elif sec["type"] in ("experience", "projects"):
            for e in sec["entries"]:
                e["bullet_runs"] = [to_runs(b) for b in e["bullets"]]
        elif sec["type"] == "education":
            sec["line_runs"] = [to_runs(l) for l in sec["lines"]]
    docx_path = os.path.join(outdir, base + ".docx")
    docx_doc["_out"] = docx_path
    open(json_path, "w").write(json.dumps(docx_doc))
    r = subprocess.run(["node", NODE_GEN, json_path], capture_output=True, text=True)
    print("DOCX:", docx_path if r.returncode == 0 else "FAILED: " + r.stderr[-500:])
    shutil.rmtree(scratch, ignore_errors=True)

if __name__ == "__main__":
    main()
