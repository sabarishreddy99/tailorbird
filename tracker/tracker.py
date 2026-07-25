#!/usr/bin/env python3
"""
Job application tracker. Stdlib only, no installs.

    python3 tracker/tracker.py

Serves a local UI at http://localhost:8765 backed by job_queue.json in the
repo root, so the queue stays in git and is readable in Claude sessions.

On first run, seeds job_queue.json from the Application History table in
resumes/_index/LIBRARY.md.
"""

import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import webbrowser
from datetime import date
from pathlib import Path
from urllib.parse import unquote

PORT = 8765
ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "job_queue.json"
LIBRARY = ROOT / "resumes" / "_index" / "LIBRARY.md"
HTML = Path(__file__).resolve().parent / "tracker.html"
AGENT_RUN = ROOT / "agent" / "run.py"
RUNS_DIR = ROOT / "agent" / "runs"
UI_RUN_LOG = RUNS_DIR / "ui-run.log"
PDFS_DIR = ROOT / "resumes" / "pdfs"


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def resolve_pdf(job):
    """Best-effort path (relative to resumes/pdfs) of a role's résumé PDF, so the
    table can link straight to it. Prefers a stored `pdf` field, else finds the
    company folder case-insensitively and the filename that best matches the role."""
    stored = job.get("pdf")
    if stored:
        p = PDFS_DIR / stored
        if p.exists():
            return stored
    if not PDFS_DIR.exists():
        return None
    company = _norm(job.get("company"))
    folder = None
    for d in PDFS_DIR.iterdir():
        if d.is_dir() and _norm(d.name) == company:
            folder = d
            break
    if not folder:
        return None
    pdfs = sorted(folder.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not pdfs:
        return None
    rslug = _norm(job.get("role"))[:14]
    if rslug:
        for p in pdfs:
            if rslug in _norm(p.stem):
                return str(p.relative_to(PDFS_DIR))
    return str(pdfs[0].relative_to(PDFS_DIR))


def tail(path, n=250):
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []

# The agent package (schedule + report helpers) lives alongside the tracker.
sys.path.insert(0, str(ROOT / "agent"))
try:
    import schedule as agent_schedule
    import report as agent_report
except Exception:  # agent package optional; UI still works without it
    agent_schedule = None
    agent_report = None

# Lifecycle beyond what LIBRARY.md tracks. LIBRARY records whether a resume
# was generated; this tracks what happened to the application afterward.
STATUSES = [
    "queued",
    "needs_review",
    "resume_ready",
    "applied",
    "screen",
    "interviewing",
    "offer",
    "rejected",
    "dropped",
]

# Fields the UI is allowed to write. Anything else in a PATCH body is ignored,
# so a stale or malformed client cannot invent columns or overwrite "id".
EDITABLE = {"company", "role", "coverage", "status", "url", "notes", "date"}


def seed_from_library():
    """Parse the Application History table into queue records."""
    if not LIBRARY.exists():
        return []
    rows = []
    in_history = False
    for line in LIBRARY.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Application History"):
            in_history = True
            continue
        if in_history and line.startswith("## "):
            break
        if not in_history or not line.startswith("| 20"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        date, company, role, coverage, outcome = cells[0], cells[1], cells[2], cells[3], cells[4]
        # LIBRARY only distinguishes saved vs dropped; map onto lifecycle.
        low = outcome.lower()
        status = "dropped" if low.startswith("dropped") or "**dropped**" in low else "resume_ready"
        rows.append(
            {
                "id": f"{date}-{re.sub(r'[^a-z0-9]+', '-', company.lower())}-{len(rows)}",
                "date": date,
                "company": company,
                "role": role,
                "coverage": coverage,
                "status": status,
                "url": "",
                "notes": re.sub(r"^(saved|dropped)\s*", "", outcome, flags=re.I).strip(" ()*"),
            }
        )
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def load():
    if QUEUE.exists():
        return json.loads(QUEUE.read_text(encoding="utf-8"))
    data = {"jobs": seed_from_library()}
    save(data)
    return data


def save(data):
    # Write to a temp file and rename, so an interrupted write can never leave
    # job_queue.json truncated or half-written.
    tmp = QUEUE.with_name(QUEUE.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, QUEUE)


def rev():
    """Revision marker for the data file, so the UI can spot outside edits."""
    return QUEUE.stat().st_mtime_ns if QUEUE.exists() else 0


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self):
        """Read a JSON request body. Returns (obj, error_string)."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length) or b"{}"), None
        except json.JSONDecodeError:
            return None, "bad json"

    def _job_id(self):
        m = re.match(r"^/api/jobs/([^/]+)$", self.path)
        return m.group(1) if m else None

    def _serve_pdf(self, rel):
        """Serve a résumé PDF from resumes/pdfs, inline so it opens in a tab.
        Resolved paths must stay inside PDFS_DIR (no traversal)."""
        rel = unquote(rel).split("?")[0]
        try:
            target = (PDFS_DIR / rel).resolve()
            target.relative_to(PDFS_DIR.resolve())  # raises if outside
        except (ValueError, OSError):
            self._send(403, json.dumps({"error": "forbidden"}))
            return
        if not target.exists() or target.suffix.lower() != ".pdf":
            self._send(404, json.dumps({"error": "no such pdf"}))
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'inline; filename="{target.name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/jobs":
            data = load()
            # Attach a résumé PDF link + absolute disk path per row
            # (transient; not persisted to disk).
            for j in data.get("jobs", []):
                pdf = resolve_pdf(j)
                if pdf:
                    j["pdf"] = pdf
                    j["pdf_abs"] = str((PDFS_DIR / pdf).resolve())
            data["rev"] = rev()
            self._send(200, json.dumps(data, ensure_ascii=False))
        elif self.path.startswith("/pdf/"):
            self._serve_pdf(self.path[len("/pdf/"):])
        elif self.path == "/api/rev":
            # Cheap poll target so the UI can notice edits made outside it
            # (the resume-tailoring skill writes via tracker/update_queue.py).
            self._send(200, json.dumps({"rev": rev(), "count": len(load()["jobs"])}))
        elif self.path == "/api/statuses":
            self._send(200, json.dumps(STATUSES))
        elif self.path == "/api/agent-status":
            status = agent_report.read_status() if agent_report else {"state": "idle", "results": []}
            self._send(200, json.dumps(status, ensure_ascii=False))
        elif self.path == "/api/schedule":
            if agent_schedule:
                self._send(200, json.dumps(agent_schedule.status()))
            else:
                self._send(200, json.dumps({"installed": False, "loaded": False,
                                            "error": "agent package not found"}))
        elif self.path == "/api/agent-log":
            # Live console tail of the current/most-recent run.
            status = agent_report.read_status() if agent_report else {}
            run_id = status.get("run_id")
            log_file = RUNS_DIR / f"{run_id}.log" if run_id else None
            if not (log_file and log_file.exists()):
                # Fall back to the newest .log so the pane is never blank.
                logs = sorted(RUNS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)
                log_file = logs[-1] if logs else None
            self._send(200, json.dumps({
                "run_id": run_id,
                "state": status.get("state", "idle"),
                "totals": status.get("totals", {}),
                "lines": tail(log_file) if log_file else [],
            }, ensure_ascii=False))
        elif self.path == "/api/runs":
            runs = sorted(RUNS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            self._send(200, json.dumps([
                {"id": p.stem, "mtime": int(p.stat().st_mtime)} for p in runs[:50]
            ]))
        elif re.match(r"^/api/runs/[\w.-]+$", self.path):
            rid = self.path.rsplit("/", 1)[-1]
            f = RUNS_DIR / f"{rid}.md"
            if f.exists():
                self._send(200, json.dumps({"id": rid, "markdown": f.read_text(encoding="utf-8")},
                                           ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "no such run"}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def _run_agent(self):
        """Spawn agent/run.py detached. run.py's own lock prevents overlap."""
        body, _ = self._body()
        body = body or {}
        conc = str(int(body.get("concurrency", 3) or 3))
        args = [sys.executable, str(AGENT_RUN), "--once", "--concurrency", conc]
        if body.get("dry_run"):
            args.append("--dry-run")
        if not AGENT_RUN.exists():
            self._send(404, json.dumps({"error": "agent/run.py not found"}))
            return
        UI_RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        logfh = open(UI_RUN_LOG, "a")
        subprocess.Popen(args, cwd=str(ROOT), stdout=logfh, stderr=logfh,
                         start_new_session=True)
        self._send(202, json.dumps({"ok": True, "started": True, "concurrency": conc}))

    def _schedule(self):
        if not agent_schedule:
            self._send(500, json.dumps({"error": "agent package not found"}))
            return
        body, err = self._body()
        if err:
            self._send(400, json.dumps({"error": err}))
            return
        action = (body or {}).get("action")
        if action == "enable":
            times = body.get("times") or None
            conc = body.get("concurrency") or None
            self._send(200, json.dumps(agent_schedule.enable(times, conc)))
        elif action == "disable":
            self._send(200, json.dumps(agent_schedule.disable()))
        else:
            self._send(400, json.dumps({"error": "action must be enable|disable"}))

    def do_POST(self):
        """Create one job, or trigger the agent / manage its schedule."""
        if self.path == "/api/run-agent":
            self._run_agent()
            return
        if self.path == "/api/schedule":
            self._schedule()
            return
        if self.path != "/api/jobs":
            self._send(404, json.dumps({"error": "not found"}))
            return
        body, err = self._body()
        if err:
            self._send(400, json.dumps({"error": err}))
            return
        data = load()
        jobs = data["jobs"]
        row = {k: body.get(k, "") for k in EDITABLE}
        row["status"] = row["status"] or "queued"
        row["date"] = row["date"] or date.today().isoformat()
        row["id"] = body.get("id") or f"{row['date']}-{os.urandom(3).hex()}"
        jobs.insert(0, row)
        save(data)
        self._send(201, json.dumps({"ok": True, "job": row, "rev": rev(), "count": len(jobs)}))

    def do_PATCH(self):
        """Update named fields on ONE job, leaving every other row untouched."""
        jid = self._job_id()
        if not jid:
            self._send(404, json.dumps({"error": "not found"}))
            return
        body, err = self._body()
        if err:
            self._send(400, json.dumps({"error": err}))
            return
        data = load()
        for j in data["jobs"]:
            if j.get("id") == jid:
                for k, v in body.items():
                    if k in EDITABLE:
                        j[k] = v
                save(data)
                self._send(200, json.dumps({"ok": True, "job": j, "rev": rev()}))
                return
        self._send(404, json.dumps({"error": f"no job with id {jid}"}))

    def do_DELETE(self):
        jid = self._job_id()
        if not jid:
            self._send(404, json.dumps({"error": "not found"}))
            return
        data = load()
        before = len(data["jobs"])
        data["jobs"] = [j for j in data["jobs"] if j.get("id") != jid]
        if len(data["jobs"]) == before:
            self._send(404, json.dumps({"error": f"no job with id {jid}"}))
            return
        save(data)
        self._send(200, json.dumps({"ok": True, "rev": rev(), "count": len(data["jobs"])}))

    def do_PUT(self):
        # Removed deliberately. This used to replace the whole file with
        # whatever the browser held, so a tab with stale state silently erased
        # rows written by tracker/update_queue.py. Use POST/PATCH/DELETE.
        self._send(409, json.dumps({
            "error": "whole-file PUT is no longer supported; reload the page to "
                     "get the current UI, which saves one row at a time"
        }))

    def log_message(self, *args):
        pass  # quiet


if __name__ == "__main__":
    load()  # seed on first run
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        url = f"http://localhost:{PORT}"
        print(f"Job tracker running at {url}")
        print(f"Data file: {QUEUE}")
        print("Ctrl-C to stop.")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
