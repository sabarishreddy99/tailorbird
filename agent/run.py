#!/usr/bin/env python3
"""
run.py - the resume-queue agent orchestrator.

Pure-Python control loop that turns queued job URLs into tailored resumes with
no tokens spent on mechanical work and no permission prompts. For each queued
role it:

  1. fetches the JD (agent/ats.py, any host, in Python)      -- parallel, pool<=8
  2. screens eligibility/fit (agent/screen.py, regex)        -- parallel
       HARD_DROP    -> tracker dropped + LIBRARY history row
       NEEDS_REVIEW -> tracker needs_review + reason (parked for the user)
       BUILD        -> step 3
  3. runs the REAL /resume-tailoring skill headlessly to author + build the
     resume                                                   -- parallel, pool<=N
  4. commits shared-file writes (job_queue.json, LIBRARY.md) in the PARENT,
     serialized, so parallel authoring can never corrupt them.

The skill (step 3) owns all tailoring quality (research, matching, one-page,
truthfulness boundaries). It writes only per-company resume files plus a small
staging JSON; the orchestrator does the shared commits. Quality is identical to
a sequential run because each role gets its own dedicated skill invocation.

    python3 agent/run.py --once [--concurrency N]
    python3 agent/run.py --dry-run
    python3 agent/run.py --only <url>
"""

import argparse
import concurrent.futures as cf
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ats
import screen
import report
import library

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "job_queue.json"
LIBRARY = ROOT / "resumes" / "_index" / "LIBRARY.md"
UPDATE_QUEUE = ROOT / "tracker" / "update_queue.py"
RUNLOCK = ROOT / "agent" / ".lock"
STAGING = ROOT / "agent" / "runs" / "staging"

FETCH_POOL = 8
SKILL_TIMEOUT = 900  # 15 min per role

LOGFILE = None  # set once run_id is known; the Logs tab tails this per-run file.


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if LOGFILE:
        try:
            with open(LOGFILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "role"


def claude_bin():
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def load_queued(only_url=None):
    data = json.loads(QUEUE.read_text(encoding="utf-8"))
    rows = data.get("jobs", [])
    if only_url:
        return [r for r in rows if r.get("url") == only_url]
    return [r for r in rows if r.get("status") == "queued" and r.get("url")]


# ------------------------------------------------------- shared-file commits
# All of these run in the PARENT thread only, so they are naturally serialized.

def find_pdf(company):
    """Newest résumé PDF for a company, as a path relative to resumes/pdfs."""
    base = ROOT / "resumes" / "pdfs"
    norm = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    for d in base.iterdir() if base.exists() else []:
        if d.is_dir() and re.sub(r"[^a-z0-9]", "", d.name.lower()) == norm:
            pdfs = sorted(d.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
            if pdfs:
                return str(pdfs[0].relative_to(base))
    return ""


def upsert(company, role, status, url, coverage="", notes="", pdf=""):
    cmd = [sys.executable, str(UPDATE_QUEUE), "--company", company or "unknown",
           "--status", status, "--url", url]
    if role:
        cmd += ["--role", role]
    if coverage:
        cmd += ["--coverage", coverage]
    if notes:
        cmd += ["--notes", notes]
    if pdf:
        cmd += ["--pdf", pdf]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  update_queue failed: {r.stderr.strip()[:200]}")
    return r.returncode == 0


def append_history(row_md):
    if not row_md:
        return
    row_md = row_md.strip()
    if not row_md.startswith("|"):
        row_md = "| " + row_md
    with open(LIBRARY, "a", encoding="utf-8") as fh:
        fh.write("\n" + row_md + "\n")


def drop_history_row(company, role, reason):
    today = date.today().isoformat()
    return (f"| {today} | {company} | {role or ''} | n/a | dropped "
            f"(agent: {reason}) |")


# ---------------------------------------------------------------- stage A/B

def fetch_and_screen(row):
    job = ats.fetch(row["url"])
    verdict = screen.classify(job)
    return row, job, verdict


def style_violations(md_rel):
    """Deterministic quality gate (0 tokens): the checks the vision pass used to
    do. Returns a list of problems found in the produced resume .md."""
    if not md_rel:
        return []
    p = ROOT / md_rel
    if not p.exists():
        return ["resume .md not found at reported path"]
    text = p.read_text(encoding="utf-8", errors="replace")
    out = []
    if "—" in text:  # em dash
        out.append("em-dash present")
    for line in text.splitlines():
        m = re.match(r"\s*\*\*[^*]+:\*\*(.*)", line)   # a "**Category:**" skills line
        if m and "**" in m.group(1):
            out.append("literal ** inside a skills line")
            break
    return out


def parse_usage(stdout):
    """Best-effort telemetry from `claude -p --output-format json`.

    Reports the meaningful numbers: `cost` (USD) and `new_tok` = fresh input +
    output + cache-creation (full-price tokens). Cache *reads* are counted
    separately as `cache_tok` because they are cheap (~0.1x) and, in an agentic
    loop, balloon far above the real work if summed into one 'tokens' figure.
    """
    try:
        env = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return {}
    u = env.get("usage") or {}
    inp = u.get("input_tokens", 0) or 0
    out = u.get("output_tokens", 0) or 0
    cc = u.get("cache_creation_input_tokens", 0) or 0
    cr = u.get("cache_read_input_tokens", 0) or 0
    return {
        "new_tok": inp + out + cc,
        "cache_tok": cr,
        "sec": round((env.get("duration_ms") or 0) / 1000),
        "turns": env.get("num_turns"),
        "cost": env.get("total_cost_usd"),
    }


def run_skill(row, job, run_id):
    """Invoke the real skill headlessly for one BUILD role over a compact kit.
    Returns {outcome, company, role, coverage, reason, history_row, telem}."""
    STAGING.mkdir(parents=True, exist_ok=True)
    s = slug((job.get("title") or row.get("company") or "role"))
    jd_path = STAGING / f"{run_id}-{s}.jd.txt"
    kit_path = STAGING / f"{run_id}-{s}.kit.md"
    out_path = STAGING / f"{run_id}-{s}.json"
    jd_path.write_text(job.get("text", ""), encoding="utf-8")
    if out_path.exists():
        out_path.unlink()

    # Pre-assemble the skill's own token-efficient inputs (pure Python, 0 tokens).
    kit = library.build_kit(job.get("text", ""), row.get("company", ""))
    kit_path.write_text(kit["kit_text"], encoding="utf-8")
    research = ("Do the skill's EXPAND research for this role (unfamiliar company / new "
                "archetype / low projected coverage)."
                if kit["research"] else
                "SKIP web research: this maps cleanly to a known archetype and familiar "
                "company. Tailor from the JD + kit unless you find a real gap.")

    prompt = f"""Use the /resume-tailoring:resume-tailoring skill to tailor ONE resume in
AUTONOMOUS EXPRESS mode (no interactive checkpoints; take the skill's recommended option
and proceed).

Role URL: {row['url']}
Company hint: {row.get('company','')}
Job description (full text): {jd_path}
Authoring kit (candidate facts, archetype table, boundaries, style rules, and the CHOSEN
BASE RESUME to delta-edit): {kit_path}

Follow the skill for tailoring quality, but take its token-efficient path:
- Everything you need is in the kit. Do NOT open resumes/_index/LIBRARY.md and do NOT
  scan the resumes/ folder.
- Start from the CHOSEN BASE RESUME in the kit and delta-edit it (switch base only if the
  archetype table shows a clearly better fit).
- {research}
- Build with resumes/_assets/build_resume.py and TRUST its text output for the one-page
  check ("N page(s)", "UNDERFILLED", "WARNING"). Do NOT render or read screenshots.
- Enforce the style rules: one tight page, NO em dashes, no literal ** inside skills
  lines, respect every Truthfulness Boundary.
- Save md + report to resumes/{{Company}}/ and the pdf to resumes/pdfs/{{Company}}/, using a
  Title-Case company folder (e.g. resumes/Brex/, NOT resumes/brex/) to match the library.

Concurrency: do NOT edit job_queue.json and do NOT append to LIBRARY.md. When done, write
ONLY this JSON file:
{out_path}
keys:
  outcome     "built" | "needs_review"
  company     proper company name (resumes/ folder name)
  role        role title
  coverage    e.g. "~85%"
  reason      one line
  history_row the single LIBRARY Application-History row you would have appended
              (start "| {date.today().isoformat()} | ...").
  md_path     path (relative to repo root) of the resume .md you wrote
  pages       page count reported by build_resume.py (int)

If you cannot honestly build it (ambiguous eligibility, a central-language absence, or
projected coverage below ~65%), set outcome="needs_review" with the reason and still write
the JSON. Never fabricate experience."""

    cmd = [claude_bin(), "-p", "--output-format", "json",
           "--permission-mode", "bypassPermissions", "--add-dir", str(ROOT)]
    try:
        res = subprocess.run(cmd, input=prompt, cwd=str(ROOT), capture_output=True,
                             text=True, timeout=SKILL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"outcome": "failed", "reason": "skill run timed out", "telem": {}}

    telem = parse_usage(res.stdout)
    if not out_path.exists():
        return {"outcome": "failed", "reason": "skill produced no result file", "telem": telem}
    try:
        out = json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"outcome": "failed", "reason": "unreadable skill result file", "telem": telem}
    out["telem"] = telem

    # Deterministic post-build gate (replaces the vision check).
    if out.get("outcome") == "built":
        problems = list(style_violations(out.get("md_path")))
        if out.get("pages") and out["pages"] != 1:
            problems.append(f"{out['pages']} pages, not one")
        if problems:
            out["outcome"] = "needs_review"
            out["reason"] = "quality gate: " + "; ".join(problems)
    return out


# --------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", default=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only", default=None, help="process a single URL")
    p.add_argument("--concurrency", type=int, default=3)
    a = p.parse_args()

    RUNLOCK.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(RUNLOCK, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another agent run is already in progress; exiting.")
        return 0

    run_id = report.now_id()
    global LOGFILE
    LOGFILE = ROOT / "agent" / "runs" / f"{run_id}.log"
    LOGFILE.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now().isoformat(timespec="seconds")
    rows = load_queued(a.only)
    log(f"run {run_id}: {len(rows)} queued role(s), concurrency={a.concurrency}"
        + (" [DRY RUN]" if a.dry_run else ""))
    if not rows:
        report.write_status("idle", run_id, [], started=started)
        return 0

    # Stage A/B: fetch + screen in parallel.
    triples = []
    with cf.ThreadPoolExecutor(max_workers=FETCH_POOL) as ex:
        for row, job, verdict in ex.map(fetch_and_screen, rows):
            triples.append((row, job, verdict))
            log(f"  screen: {row.get('company','?'):22} -> {verdict['verdict']:12} "
                f"| {verdict['reason'][:60]}")

    if a.dry_run:
        print("\n{:<22} {:<12} {:<10} {}".format("COMPANY", "VERDICT", "COMP", "REASON"))
        for row, job, v in triples:
            comp = f"${v['comp'][1]//1000}K" if v.get("comp") else "-"
            print("{:<22} {:<12} {:<10} {}".format(
                (row.get("company") or "?")[:22], v["verdict"], comp, v["reason"][:70]))
        report.write_status("idle", run_id, [], started=started)
        return 0

    report.write_status("running", run_id, [], started=started)
    results = []
    builds = []

    # Handle drops + needs_review immediately (fast, parent-serialized).
    for row, job, v in triples:
        company = row.get("company") or "unknown"
        role = row.get("role") or (job.get("title") or "").strip()
        if v["verdict"] == "HARD_DROP":
            upsert(company, role, "dropped", row["url"], notes=f"agent: {v['reason']}")
            append_history(drop_history_row(company, role, v["reason"]))
            results.append({"company": company, "role": role, "url": row["url"],
                            "outcome": "dropped", "reason": v["reason"]})
        elif v["verdict"] == "NEEDS_REVIEW":
            upsert(company, role, "needs_review", row["url"], notes=f"agent: {v['reason']}")
            results.append({"company": company, "role": role, "url": row["url"],
                            "outcome": "needs_review", "reason": v["reason"]})
        else:
            builds.append((row, job, v))

    # Stage B commits: author in parallel, commit in the parent as each finishes.
    if builds:
        log(f"  building {len(builds)} role(s) with the skill...")
        with cf.ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
            fut = {ex.submit(run_skill, row, job, run_id): (row, job)
                   for row, job, _ in builds}
            for f in cf.as_completed(fut):
                row, job = fut[f]
                company = row.get("company") or "unknown"
                role = row.get("role") or (job.get("title") or "").strip()
                try:
                    out = f.result()
                except Exception as e:
                    out = {"outcome": "failed", "reason": f"worker error: {e}"}
                outcome = out.get("outcome", "failed")
                company = out.get("company") or company
                role = out.get("role") or role
                cov = out.get("coverage", "")
                reason = out.get("reason", "")
                telem = out.get("telem") or {}
                if outcome == "built":
                    upsert(company, role, "resume_ready", row["url"], coverage=cov,
                           notes=f"agent: {reason}"[:300], pdf=find_pdf(company))
                    append_history(out.get("history_row", ""))
                elif outcome == "needs_review":
                    upsert(company, role, "needs_review", row["url"], notes=f"agent: {reason}"[:300])
                else:  # failed -> leave the row queued for the next run
                    log(f"  build FAILED {company}: {reason}")
                results.append({"company": company, "role": role, "url": row["url"],
                                "outcome": outcome, "reason": reason, "coverage": cov,
                                "tokens": telem.get("new_tok"), "cache_tokens": telem.get("cache_tok"),
                                "cost": telem.get("cost"), "seconds": telem.get("sec"),
                                "turns": telem.get("turns")})
                bits = []
                if telem.get("cost") is not None:
                    bits.append(f"${telem['cost']:.2f}")
                if telem.get("new_tok"):
                    bits.append(f"{telem['new_tok'] // 1000}k new tok")
                if telem.get("sec"):
                    bits.append(f"{telem['sec']}s")
                extra = (" · " + " · ".join(bits)) if bits else ""
                log(f"  {outcome.upper():12} {company} {('('+cov+')') if cov else ''}{extra}")

    log_path = report.write_runlog(run_id, results)
    report.write_status("idle", run_id, results, started=started, log_path=log_path)
    c = {v: sum(1 for r in results if r["outcome"] == v)
         for v in ("built", "dropped", "needs_review", "failed")}
    tot_tok = sum(r.get("tokens") or 0 for r in results)
    tot_sec = sum(r.get("seconds") or 0 for r in results)
    tot_cost = sum(r.get("cost") or 0 for r in results)
    tel = []
    if tot_cost:
        tel.append(f"${tot_cost:.2f}")
    if tot_tok:
        tel.append(f"{tot_tok // 1000}k new tok")
    if tot_sec:
        tel.append(f"{tot_sec}s skill time")
    log(f"done: {c['built']} built, {c['dropped']} dropped, {c['needs_review']} need "
        f"review, {c['failed']} failed"
        + (" · " + " · ".join(tel) if tel else "")
        + f" -> {log_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
