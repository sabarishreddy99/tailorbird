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
import session

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "job_queue.json"
LIBRARY = ROOT / "resumes" / "_index" / "LIBRARY.md"
UPDATE_QUEUE = ROOT / "tracker" / "update_queue.py"
RUNLOCK = ROOT / "agent" / ".lock"
STAGING = ROOT / "agent" / "runs" / "staging"
# Primed-base state (id, model, source fingerprints, freshness) lives in
# agent/session.py so the tracker UI can read and act on it too.

FETCH_POOL = 8
SKILL_TIMEOUT = 900  # 15 min per role
# Reasoning effort for the authoring session. None = inherit the CLI default
# (unchanged behaviour). Opus reasoning tokens bill as OUTPUT, so this is the
# largest single cost lever -- but it is also the only one that can plausibly
# move quality, so it stays opt-in via --effort until an A/B says otherwise.
EFFORT = None
# Authoring model. Pinned because --setting-sources drops the user's
# settings.json (which is where "model": "opus" lived); without this the CLI
# default silently resolved to Sonnet.
MODEL = "opus"


def resolve_model(name):
    """'inherit' means: use whatever ~/.claude/settings.json says, but pass it
    EXPLICITLY. We cannot just omit --model, because --setting-sources drops the
    file that defines it and the CLI default is not the user's choice."""
    if name != "inherit":
        return name
    try:
        cfg = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        return cfg.get("model") or "opus"
    except Exception:
        return "opus"

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


def load_queued(only=None):
    """Queued rows that have something to work from: a URL, or a pasted JD.

    `only` matches a URL or a row id, so a pasted-JD row (which may have no URL
    at all) can still be targeted directly.
    """
    data = json.loads(QUEUE.read_text(encoding="utf-8"))
    rows = data.get("jobs", [])
    if only:
        return [r for r in rows if r.get("url") == only or r.get("id") == only]
    return [r for r in rows
            if r.get("status") == "queued" and (r.get("url") or r.get("jd_file"))]


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


def upsert(company, role, status, url, coverage="", notes="", pdf="", job_id=""):
    cmd = [sys.executable, str(UPDATE_QUEUE), "--company", company or "unknown",
           "--status", status, "--url", url]
    if job_id:
        # Exact identity. Rows built from a pasted JD may have no URL, and
        # company-matching alone could hit a different role at the same company.
        cmd += ["--id", job_id]
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

def pasted_jd(row):
    """Job description the user pasted in the UI, if any (agent/jds/<id>.txt)."""
    rel = row.get("jd_file") or (f"agent/jds/{row['id']}.txt" if row.get("id") else "")
    if not rel:
        return ""
    try:
        return (ROOT / rel).read_text(encoding="utf-8")
    except Exception:
        return ""


def fetch_and_screen(row):
    # A pasted JD wins over the URL: the user supplies one precisely because the
    # fetcher could not read that posting, so re-fetching would only overwrite
    # good text with an empty shell.
    text = pasted_jd(row)
    if text.strip():
        job = ats.from_text(text, title=row.get("role", ""), url=row.get("url", ""))
    else:
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
        # Split out of the new_tok lump: on Opus, reasoning bills as OUTPUT, so
        # this is usually the single largest cost line and was invisible before.
        "out_tok": out,
        "cc_tok": cc,
        "in_tok": inp,
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
    if name.startswith("mcp__"):
        return f"🔌 portfolio lookup: {name.split('__')[-1]}"
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
    result_evt, counts, tail = None, {"research": 0, "builds": 0, "mcp": 0, "tools": {}}, []
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
                        # Which tools burn the turns? (edit churn was invisible before)
                        counts["tools"][name] = counts["tools"].get(name, 0) + 1
                        if name.startswith("mcp__"):
                            counts["mcp"] += 1
                        if name in ("WebSearch", "WebFetch"):
                            counts["research"] += 1
                        elif name == "Bash" and "build_resume.py" in (inp.get("command") or ""):
                            counts["builds"] += 1
                            # build_resume.py now reports the fit gap in lines, so one
                            # sized edit should land it. A 3rd build means the edit was
                            # not sized to the number -- surface it instead of silently
                            # burning turns (this loop cost $3.02/50 turns on one role).
                            if counts["builds"] == 3:
                                log(f"    [{company}] ⚠️  3rd PDF build - fit-gap convergence "
                                    f"is not working; check the FIT line handling")
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
    them or the cached prefix (system prompt + skill) won't be reused.

    --setting-sources project: without it the CLI loads ~/.claude/settings.json,
    whose enabled plugins (frontend-design, vercel, figma) inject every one of
    their skill descriptions into the system prompt of every session. Measured on
    a no-op prompt: 19.6k -> 13.2k prefix tokens. That prefix is re-read on every
    turn, so it pays back per turn, for every role in the queue.

    --model is then REQUIRED, not optional: dropping user settings also drops
    their `"model": "opus"`, and the CLI default resolved to Sonnet. Pinning it
    keeps authoring on exactly the model it ran on before. Verified by asking
    each configuration which model it was running as.
    """
    cmd = ["--strict-mcp-config", "--permission-mode", "bypassPermissions",
           "--add-dir", str(ROOT), "--setting-sources", "project",
           "--model", MODEL]
    # --strict-mcp-config means "load ONLY what this flag names", so the servers
    # in agent/mcp.json are the complete set - nothing is auto-discovered from
    # ~/.claude.json or a repo .mcp.json. Passed to the prime AND every fork
    # because tools render at the very front of the prompt: a mismatch here
    # would invalidate the whole cached prefix rather than just adding tools.
    if session.MCP_CONFIG.exists():
        cmd += ["--mcp-config", str(session.MCP_CONFIG)]
    if EFFORT:
        cmd += ["--effort", EFFORT]
    return cmd


def prime_session(sid):
    """Warm a persistent base session that loads the skill exactly once AND
    carries the role-invariant authoring context for the whole queue.

    The candidate facts / boundaries / style rules are byte-identical for every
    role, so holding them here makes them a cache READ per job instead of ~8k
    tokens of cache CREATION re-paid on every single resume.
    """
    mcp = ""
    if session.MCP_CONFIG.exists() and session.mcp_servers():
        mcp = (
            "\n\n## Live portfolio lookup (MCP: "
            + ", ".join(session.mcp_servers()) + ")\n"
            "You also have mcp__portfolio__* tools backed by the candidate's own live "
            "portfolio API (experience, projects, skills, education, blog, apps).\n"
            "Use it SPARINGLY and only to look up a specific fact the per-role kit does "
            "not carry, e.g. the detail of a project the JD asks about. Prefer "
            "search_knowledge for open questions and the get_* tools for structured facts.\n"
            "IMPORTANT: the Truthfulness Boundaries above still govern everything. The "
            "portfolio is a lookup, not permission to widen a claim: if the boundaries say "
            "a technology is NOT held, it stays absent from the resume no matter what the "
            "portfolio returns. Never let a lookup introduce a claim the boundaries "
            "exclude, and never spend a call on something the kit already answers.")

    warm = ("Load the /resume-tailoring:resume-tailoring skill so it is ready for use.\n\n"
            "Then hold the following context for every resume you author in this session. "
            "You will receive one small per-role kit per job; everything below is shared "
            "and will NOT be repeated, so do not ask for it again and do not open "
            "resumes/_index/LIBRARY.md.\n\n"
            + library.build_prime_context() + mcp +
            "\n\nReply with exactly READY and nothing else. Do not tailor anything yet.")
    cmd = [claude_bin(), "-p", "--session-id", sid, "--output-format", "json"] + _base_cmd()
    try:
        r = subprocess.run(cmd, input=warm, cwd=str(ROOT), capture_output=True,
                           text=True, timeout=300)
        return r.returncode == 0
    except Exception:
        return False


def ensure_base_session(force=False):
    """Return a persistent base session id with the skill + shared candidate
    context already loaded, priming once if needed. Returns None if priming fails
    (caller falls back to fresh sessions, still MCP-free).

    Reused across jobs AND across runs until session.status() says it is stale -
    which covers edited skill files, an edited LIBRARY.md, or a switch of model
    or effort (prompt caches are model-scoped, so reusing a session primed on a
    different model would silently throw the cached prefix away).
    """
    st = session.status(model=MODEL, effort=EFFORT)
    if not force and not st["stale"]:
        log(f"  reusing primed session {st['short_id']}… "
            f"(model={st['model']}, {st['uses']} role(s) served, "
            f"cache {'warm' if st['cache_warm'] else 'cold'})")
        return st["session_id"]
    for why in st["reasons"]:
        log(f"  re-prime needed: {why}")
    sid = str(uuid.uuid4())
    log(f"  priming session {sid[:8]}… (model={MODEL}, effort={EFFORT or 'default'}; "
        f"re-reading skill + LIBRARY from disk, one time for the whole queue)")
    if not prime_session(sid):
        log("  prime failed; falling back to a fresh session per job (still MCP-free).")
        return None
    session.write(sid, MODEL, EFFORT)
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
    # The role-INVARIANT half already lives in the primed base session; only fold
    # it back in when running without one, or the author would lose the
    # truthfulness boundaries and style rules entirely.
    kit = library.build_job_kit(job.get("text", ""), row.get("company", ""),
                                job.get("title", "") or row.get("role", ""))
    kit_text = kit["kit_text"]
    if not base_uuid:
        kit_text = library.build_prime_context() + "\n\n---\n\n" + kit_text
    kit_path.write_text(kit_text, encoding="utf-8")
    research = ("Research ONLY what the JD itself does not tell you and that materially "
                "affects tailoring (an unfamiliar product/team, domain jargon, or a genuine "
                "coverage gap). The full JD is already provided, so do NOT spend searches on "
                "generic company background — keep it to 1-2 targeted lookups at most."
                if kit["research"] else
                "SKIP web research: this maps cleanly to a known archetype and familiar "
                "company. Tailor from the JD + kit unless you find a real gap.")

    skill_intro = (
        "Using the /resume-tailoring:resume-tailoring skill and the shared candidate "
        "context ALREADY LOADED in this session (do NOT re-open or re-read the skill "
        "files or the candidate facts/boundaries/style rules — they are already in "
        "context), tailor ONE resume"
        if base_uuid else
        "Use the /resume-tailoring:resume-tailoring skill to tailor ONE resume")
    prompt = f"""{skill_intro} in AUTONOMOUS EXPRESS mode (no interactive checkpoints; take the
skill's recommended option and proceed).

Role URL: {row['url']}
Company hint: {row.get('company','')}
Job description (full text): {jd_path}
Per-role kit (scored archetype table + the CHOSEN BASE RESUME to delta-edit): {kit_path}

Follow the skill for tailoring quality, but take its token-efficient path:
- The JD and the kit are the only two files you need to read. Do NOT open
  resumes/_index/LIBRARY.md and do NOT list or scan the resumes/ folder.
- Start from the CHOSEN BASE RESUME in the kit and delta-edit it. The kit's archetype
  table is scored but keyword-based and is wrong roughly a third of the time, so if the
  JD clearly fits another row, switch to it and read ONLY that one file.
- {research}
- Build with resumes/_assets/build_resume.py and TRUST its text output. It prints ONE
  quantified fit line giving the gap in rendered lines:
    FIT: ... slack ~N line(s). Ship it        -> done; do NOT edit or rebuild again.
    OVERFULL: ~N line(s) too long             -> cut ~N rendered lines, ONE edit, rebuild once.
    UNDERFILLED: room for ~N more line(s)     -> add ~N rendered lines, ONE edit, rebuild once.
  A rendered line is ~110 characters of bullet text. Apply the whole correction in ONE
  tool call: use MultiEdit, or re-Write the file, rather than a run of single Edit calls
  (each one is a separate turn and they are the main cost of a resume). Then rebuild.
  Budget: at most 2 builds and ~10 tool calls total. Do NOT re-read a file you just
  wrote, and do NOT render or read screenshots.
- Enforce the style rules: one tight page, NO em dashes, no literal ** inside skills
  lines, respect every Truthfulness Boundary.
- Save the md to resumes/{{Company}}/ and the pdf to resumes/pdfs/{{Company}}/, using a
  Title-Case company folder (e.g. resumes/Brex/, NOT resumes/brex/) to match the library.
  Do NOT write a _Report.md file: it is rendered for you from the JSON below.

Concurrency: do NOT edit job_queue.json and do NOT append to LIBRARY.md. When done, write
ONLY this JSON file (ONE write, no separate report file):
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
  report      object with ONLY your judgment (the scaffolding, dates, paths and
              headings are filled in for you - do not restate them):
                focus_areas        [str]  what this role actually centres on
                key_requirements   [str]  the JD's top requirements
                coverage_breakdown {{"direct": N, "transferable": N, "adjacent": N}}
                reframing          [str]  "what changed -> why", one per line
                gaps               [str]  unmet requirements and how you handled them
                differentiators    [str]  why this candidate fits
                interview_prep     [str]  stories to prepare, questions to expect

If you cannot honestly build it (ambiguous eligibility, a central-language absence, or
projected coverage below ~65%), set outcome="needs_review" with the reason and still write
the JSON. Never fabricate experience."""

    company = row.get("company") or (job.get("title") or "role")
    # Surface the pick so mis-picks are measurable rather than invisible: if the
    # author keeps switching away from the top row, the scoring needs work.
    log(f"    [{company}] archetype: {kit.get('archetype')} [{kit.get('confidence', 0):.2f}] "
        f"-> {kit.get('base_rel')}  (runner-up {kit.get('runner_up_score') or 0:.2f}: "
        f"{kit.get('runner_up')})")
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
    telem["mcp"] = counts["mcp"]
    telem["tools"] = counts["tools"]
    telem["effort"] = EFFORT or "default"
    telem["kit_tok"] = len(kit_text) // 4
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
    out["base_rel"] = kit.get("base_rel")

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
    global EFFORT, MODEL
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", default=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only", default=None, help="process a single URL")
    p.add_argument("--concurrency", type=int, default=3,
                   help="(kept for compatibility; authoring now runs serially through one "
                        "primed session)")
    p.add_argument("--fresh-session", action="store_true",
                   help="rollback: a fresh MCP-free session per job (no primed-base reuse)")
    p.add_argument("--effort", default=None,
                   choices=["low", "medium", "high", "xhigh", "max"],
                   help="reasoning effort for authoring (default: inherit the CLI's). "
                        "Reasoning tokens bill as output, so this is the biggest cost "
                        "lever - A/B it against the golden set before making it the norm.")
    p.add_argument("--model", default=MODEL,
                   help="authoring model, or 'inherit' to use settings.json "
                        "(default: %(default)s)")
    p.add_argument("--reprime", action="store_true",
                   help="force a fresh prime before the queue, re-reading the "
                        "skill and LIBRARY from disk")
    p.add_argument("--reprime-only", action="store_true",
                   help="rebuild the primed base session from current data and "
                        "exit without processing the queue")
    a = p.parse_args()
    EFFORT, MODEL = a.effort, resolve_model(a.model)

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

    if a.reprime_only:
        # Rebuild the shared base from whatever is on disk right now. This runs
        # in a fresh process, so every source (skill files, LIBRARY.md, the
        # resume corpus) is re-read from disk - there is no stale in-process
        # cache to bust.
        st = session.status(model=MODEL, effort=EFFORT)
        log(f"refresh: rebuilding the primed base session (model={MODEL}, "
            f"effort={EFFORT or 'default'})")
        for s in st["sources"]:
            log(f"  source: {s['name']:<9} {s['files']:>3} file(s), "
                f"{s['bytes'] // 1024:>4} KB  [{s['hash']}]")
        session.invalidate()
        sid = ensure_base_session(force=True)
        if sid:
            log(f"refresh: done - base session {sid[:8]}… primed from current data")
            return 0
        log("refresh: FAILED to prime; the next run will retry")
        return 1

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
            upsert(company, role, "dropped", row["url"], notes=f"agent: {v['reason']}",
                   job_id=row.get("id", ""))
            append_history(drop_history_row(company, role, v["reason"]))
            results.append({"company": company, "role": role, "url": row["url"],
                            "outcome": "dropped", "reason": v["reason"]})
        elif v["verdict"] == "NEEDS_REVIEW":
            upsert(company, role, "needs_review", row["url"], notes=f"agent: {v['reason']}",
                   job_id=row.get("id", ""))
            results.append({"company": company, "role": role, "url": row["url"],
                            "outcome": "needs_review", "reason": v["reason"]})
        else:
            builds.append((row, job, v))

    # Stage B: author ONE job at a time through a single primed base session, so
    # the skill is loaded once and reused (cache reads) instead of re-created per
    # job. Commits happen in the parent, already serialized.
    if builds:
        base_uuid = None if a.fresh_session else ensure_base_session(force=a.reprime)
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
            if base_uuid:
                session.touch()   # drives the UI's cache-warmth + roles-served readout
            outcome = out.get("outcome", "failed")
            company = out.get("company") or company
            role = out.get("role") or role
            cov = out.get("coverage", "")
            reason = out.get("reason", "")
            telem = out.get("telem") or {}
            if outcome == "built":
                # The _Report.md is rendered here from the result JSON instead of
                # being a second model-authored file (saves a write turn per role).
                rp = report.write_report(out, out.get("base_rel"))
                if rp:
                    log(f"    [{company}] report: {rp.name}")
                upsert(company, role, "resume_ready", row["url"], coverage=cov,
                       notes=f"agent: {reason}"[:300], pdf=find_pdf(company),
                       job_id=row.get("id", ""))
                append_history(out.get("history_row", ""))
            elif outcome == "needs_review":
                upsert(company, role, "needs_review", row["url"], notes=f"agent: {reason}"[:300],
                       job_id=row.get("id", ""))
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
            # Reasoning bills as output on Opus, so this is usually the biggest
            # cost line -- keep it visible next to the lump figure.
            if telem.get("out_tok"):
                where.append(f"{telem['out_tok'] // 1000}k output")
            if telem.get("research") is not None:
                where.append(f"{telem['research']} web search(es)")
            if telem.get("builds") is not None:
                where.append(f"{telem['builds']} PDF build(s)")
            if telem.get("mcp"):
                where.append(f"{telem['mcp']} portfolio lookup(s)")
            if telem.get("turns"):
                where.append(f"{telem['turns']} turns")
            if where:
                log(f"    [{company}] breakdown: " + " · ".join(where))
            tools = telem.get("tools") or {}
            if tools:
                log(f"    [{company}] tools: " + " · ".join(
                    f"{k} x{v}" for k, v in sorted(tools.items(), key=lambda kv: -kv[1])))
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
