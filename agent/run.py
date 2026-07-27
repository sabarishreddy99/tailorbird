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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
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
SESSION_FILE = ROOT / "agent" / "runs" / "session.json"  # persistent primed base session
SKILL_INSTALL = Path.home() / ".claude" / "skills" / "resume-tailoring"

FETCH_POOL = 8
SKILL_TIMEOUT = 900  # 15 min per role

LOGFILE = None  # set once run_id is known; the Logs tab tails this per-run file.
_LOG_LOCK = threading.Lock()  # run_skill workers stream events -> concurrent log() writers.


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    with _LOG_LOCK:
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


def parse_usage(env):
    """Best-effort telemetry from the final stream-json `result` event.

    Accepts the already-parsed `result` event dict (or, for back-compat, a raw
    JSON string). Reports the meaningful numbers: `cost` (USD) and `new_tok` =
    fresh input + output + cache-creation (full-price tokens). Cache *reads* are
    counted separately as `cache_tok` because they are cheap (~0.1x) and, in an
    agentic loop, balloon far above the real work if summed into one figure.
    """
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(env, dict):
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


def _describe_tool(name, inp):
    """Map a streamed tool_use block to one short human verb, or None to skip."""
    b = os.path.basename
    if name == "WebSearch":
        return f"🔎 web search: {(inp.get('query') or '').strip()[:70]}"
    if name == "WebFetch":
        return f"🔎 fetch: {(inp.get('url') or '').strip()[:70]}"
    if name == "Read":
        return f"📖 read {b(inp.get('file_path', '') or '')}"
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"✍️  edit {b(inp.get('file_path', '') or '')}"
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        if "build_resume.py" in cmd:
            return "🖨️  building PDF/DOCX"
        return f"⚙️  {cmd[:60]}"
    if name in ("Grep", "Glob"):
        return "🔍 searching files"
    if name == "Task":
        return "🤖 sub-agent"
    return None  # Skill, TodoWrite, etc. -> noise, skip


def _stream_skill(cmd, prompt, company):
    """Run the claude CLI streaming, echoing a concise line per tool/step to the
    run log as it happens. Returns (result_event_dict_or_None, counts, killed).
    Enforces SKILL_TIMEOUT by killing the process."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=str(ROOT))
    killed = {"v": False}

    def _kill():
        killed["v"] = True
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(SKILL_TIMEOUT, _kill)
    timer.start()
    result_evt, counts, tail = None, {"research": 0, "builds": 0}, []
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            if not line.strip():
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                tail.append(line.strip()[:200])
                del tail[:-8]  # keep only the last few non-JSON lines for diagnostics
                continue
            etype = evt.get("type")
            if etype == "assistant":
                for block in (evt.get("message") or {}).get("content") or []:
                    bt = block.get("type")
                    if bt == "tool_use":
                        name, inp = block.get("name", ""), block.get("input") or {}
                        if name in ("WebSearch", "WebFetch"):
                            counts["research"] += 1
                        elif name == "Bash" and "build_resume.py" in (inp.get("command") or ""):
                            counts["builds"] += 1
                        desc = _describe_tool(name, inp)
                        if desc:
                            log(f"    [{company}] {desc}")
                    elif bt == "text":
                        t = (block.get("text") or "").strip().split("\n", 1)[0]
                        if len(t) > 2:
                            log(f"    [{company}] 💭 {t[:90]}")
            elif etype == "result":
                result_evt = evt
    finally:
        timer.cancel()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
    return result_evt, counts, killed["v"], tail


# --------------------------------------------------------- primed base session
# The queue is authored one job at a time through a single PRIMED base session:
# the /resume-tailoring skill is loaded ONCE into that base, then every job runs
# as a clean `--resume <base> --fork-session` child. So the (expensive) skill +
# system prefix is a cache READ per job instead of being re-created N times, and
# no job ever sees another job's resume. All invocations also pass
# --strict-mcp-config so the six unused MCP servers never load.

def _base_cmd():
    """Flags shared by the prime AND every job fork. MUST be identical between
    them or the cached prefix (system prompt + skill) won't be reused."""
    return ["--strict-mcp-config", "--permission-mode", "bypassPermissions",
            "--add-dir", str(ROOT)]


def _skill_hash():
    """Fingerprint the installed skill so we re-prime when it is edited."""
    if not SKILL_INSTALL.exists():
        return ""
    h = hashlib.sha256()
    for p in sorted(SKILL_INSTALL.rglob("*.md")):
        try:
            h.update(p.name.encode()); h.update(p.read_bytes())
        except Exception:
            pass
    return h.hexdigest()[:16]


def prime_session(sid):
    """Warm a persistent base session that loads the skill exactly once."""
    warm = ("Load the /resume-tailoring:resume-tailoring skill so it is ready for use, then "
            "reply with exactly READY and nothing else. Do not tailor anything yet.")
    cmd = [claude_bin(), "-p", "--session-id", sid, "--output-format", "json"] + _base_cmd()
    try:
        r = subprocess.run(cmd, input=warm, cwd=str(ROOT), capture_output=True,
                           text=True, timeout=180)
        return r.returncode == 0
    except Exception:
        return False


def ensure_base_session(force=False):
    """Return a persistent base session id with the skill already loaded, priming
    once if needed. Returns None if priming fails (caller falls back to fresh
    sessions, still MCP-free). Persisted so the base is reused across jobs and
    across runs until the installed skill changes."""
    want = _skill_hash()
    if not force and SESSION_FILE.exists():
        try:
            st = json.loads(SESSION_FILE.read_text())
            if st.get("session_id") and st.get("skill_hash") == want:
                log(f"  reusing primed session {st['session_id'][:8]}… (skill already loaded)")
                return st["session_id"]
        except Exception:
            pass
    sid = str(uuid.uuid4())
    log(f"  priming session {sid[:8]}… (loading the skill one time for the whole queue)")
    if not prime_session(sid):
        log("  prime failed; falling back to a fresh session per job (still MCP-free).")
        return None
    try:
        SESSION_FILE.write_text(json.dumps(
            {"session_id": sid, "skill_hash": want,
             "primed_at": datetime.now().isoformat(timespec="seconds")}, indent=2))
    except Exception:
        pass
    return sid


def run_skill(row, job, run_id, base_uuid=None):
    """Invoke the real skill headlessly for one BUILD role over a compact kit.
    When base_uuid is set, fork a clean child of the primed base (skill reused);
    otherwise run a fresh MCP-free session. Returns
    {outcome, company, role, coverage, reason, history_row, telem, evicted}."""
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
    research = ("Research ONLY what the JD itself does not tell you and that materially "
                "affects tailoring (an unfamiliar product/team, domain jargon, or a genuine "
                "coverage gap). The full JD is already provided, so do NOT spend searches on "
                "generic company background — keep it to 1-2 targeted lookups at most."
                if kit["research"] else
                "SKIP web research: this maps cleanly to a known archetype and familiar "
                "company. Tailor from the JD + kit unless you find a real gap.")

    skill_intro = (
        "Using the /resume-tailoring:resume-tailoring skill ALREADY LOADED in this session "
        "(do NOT re-open or re-read the skill files — they are already in context), tailor "
        "ONE resume"
        if base_uuid else
        "Use the /resume-tailoring:resume-tailoring skill to tailor ONE resume")
    prompt = f"""{skill_intro} in AUTONOMOUS EXPRESS mode (no interactive checkpoints; take the
skill's recommended option and proceed).

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

    company = row.get("company") or (job.get("title") or "role")
    if base_uuid:
        cmd = [claude_bin(), "-p", "--resume", base_uuid, "--fork-session",
               "--output-format", "stream-json", "--verbose"] + _base_cmd()
    else:
        cmd = [claude_bin(), "-p", "--output-format", "stream-json", "--verbose"] + _base_cmd()
    result_evt, counts, killed, tail = _stream_skill(cmd, prompt, company)
    if killed:
        return {"outcome": "failed", "reason": "skill run timed out", "telem": {}}

    telem = parse_usage(result_evt)
    telem["research"], telem["builds"] = counts["research"], counts["builds"]
    if not out_path.exists():
        reason = "skill produced no result file"
        if tail:
            reason += f" ({tail[-1]})"
        # Base session gone/evicted? Signal the caller to re-prime and retry once.
        evicted = bool(base_uuid) and any(
            kw in " ".join(tail).lower() for kw in
            ("no conversation", "session not found", "no session", "could not resume",
             "not found", "resume"))
        return {"outcome": "failed", "reason": reason, "telem": telem, "evicted": evicted}
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
    p.add_argument("--concurrency", type=int, default=3,
                   help="(kept for compatibility; authoring now runs serially through one "
                        "primed session)")
    p.add_argument("--fresh-session", action="store_true",
                   help="rollback: a fresh MCP-free session per job (no primed-base reuse)")
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
    mode = "fresh session/job" if a.fresh_session else "one primed session, serial"
    log(f"run {run_id}: {len(rows)} queued role(s) ({mode})"
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

    # Stage B: author ONE job at a time through a single primed base session, so
    # the skill is loaded once and reused (cache reads) instead of re-created per
    # job. Commits happen in the parent, already serialized.
    if builds:
        base_uuid = None if a.fresh_session else ensure_base_session()
        how = "reusing one primed session" if base_uuid else "fresh session each"
        log(f"  building {len(builds)} role(s) one at a time ({how})...")
        for row, job, _ in builds:
            company = row.get("company") or "unknown"
            role = row.get("role") or (job.get("title") or "").strip()
            try:
                out = run_skill(row, job, run_id, base_uuid)
                if out.get("evicted") and base_uuid:
                    log("  base session unavailable; re-priming and retrying once...")
                    base_uuid = ensure_base_session(force=True)
                    out = run_skill(row, job, run_id, base_uuid)
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
                            "turns": telem.get("turns"),
                            "research": telem.get("research"), "builds": telem.get("builds")})
            bits = []
            if telem.get("cost") is not None:
                bits.append(f"${telem['cost']:.2f}")
            if telem.get("new_tok"):
                bits.append(f"{telem['new_tok'] // 1000}k new tok")
            if telem.get("sec"):
                bits.append(f"{telem['sec']}s")
            extra = (" · " + " · ".join(bits)) if bits else ""
            log(f"  {outcome.upper():12} {company} {('('+cov+')') if cov else ''}{extra}")
            # Where did the time/tokens go? (cache-read = skill reused, not reloaded)
            where = []
            if telem.get("cache_tok"):
                where.append(f"{telem['cache_tok'] // 1000}k cache-read")
            if telem.get("new_tok"):
                where.append(f"{telem['new_tok'] // 1000}k fresh")
            if telem.get("research") is not None:
                where.append(f"{telem['research']} web search(es)")
            if telem.get("builds") is not None:
                where.append(f"{telem['builds']} PDF build(s)")
            if telem.get("turns"):
                where.append(f"{telem['turns']} turns")
            if where:
                log(f"    [{company}] breakdown: " + " · ".join(where))
            # Live status: refresh the banner/counts as each role finishes.
            report.write_status("running", run_id, results, started=started)

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
