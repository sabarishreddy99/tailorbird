#!/usr/bin/env python3
"""
Upsert one application into job_queue.json (the tracker's data file).

Called by the resume-tailoring skill after a resume is generated or a role
is dropped, so the tracker UI and LIBRARY.md stay in sync.

    python3 tracker/update_queue.py \
        --company "Kunai" \
        --role "Sr SWE Python (Houston, on-site)" \
        --status resume_ready \
        --coverage "~90%" \
        --url "https://job-boards.greenhouse.io/kunai/jobs/5189677007" \
        --notes "monolith decomposition + Kafka near 1:1 JD map"

Matching is on (company, role) case-insensitively. An existing row is updated
in place, preserving any status the user has advanced manually (e.g. if they
already moved it to "applied", passing resume_ready will NOT regress it unless
--force is given). A new row is prepended otherwise.
"""

import argparse
import fcntl
import json
import re
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "job_queue.json"
LOCK = ROOT / "job_queue.json.lock"

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


@contextmanager
def queue_lock():
    """Serialize concurrent read-modify-write cycles on job_queue.json.

    The agent runs several headless skill invocations in parallel, and each
    calls this script to upsert its row. Without a lock, two overlapping
    load()->modify->write() cycles would lose one update. An advisory flock on
    a sidecar lockfile makes the whole cycle atomic across processes.
    """
    LOCK.touch(exist_ok=True)
    with open(LOCK, "r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

# Rank for regression protection. Terminal states are handled separately.
RANK = {s: i for i, s in enumerate(STATUSES)}
TERMINAL = {"rejected", "dropped"}


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def load():
    if not QUEUE.exists():
        return {"jobs": []}
    return json.loads(QUEUE.read_text(encoding="utf-8"))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--company", required=True)
    p.add_argument("--role", default="")
    p.add_argument("--status", required=True, choices=STATUSES)
    p.add_argument("--coverage", default="")
    p.add_argument("--url", default="")
    p.add_argument("--id", default="", help="exact row id; strongest match signal")
    p.add_argument("--notes", default="")
    p.add_argument("--pdf", default="", help="résumé PDF path relative to resumes/pdfs")
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--force", action="store_true",
                   help="overwrite status even if it would move backwards")
    a = p.parse_args()

    with queue_lock():
        data = load()
        jobs = data.setdefault("jobs", [])

        # Row id is exact, so it wins when the caller knows it. This matters for
        # rows created from a PASTED job description, which may carry no URL at
        # all: without an id those fall through to company matching and can
        # silently update a different role at the same company.
        match = None
        if a.id:
            match = next((j for j in jobs if j.get("id") == a.id), None)

        # URL is the next strongest signal: a row queued from the tracker UI
        # has the URL but usually no role yet.
        if match is None and a.url:
            match = next((j for j in jobs if j.get("url") and j["url"].strip() == a.url.strip()), None)

        if match is None:
            for j in jobs:
                if norm(j.get("company")) != norm(a.company):
                    continue
                # Same company can hold several roles. Treat as the same entry when
                # either side lacks a role (the queued-URL case) or they match.
                if not a.role or not j.get("role") or norm(j["role"]) == norm(a.role):
                    match = j
                    break

        if match:
            old = match.get("status", "queued")
            # Don't silently undo progress the user recorded in the UI.
            regress = (
                not a.force
                and old not in TERMINAL
                and a.status not in TERMINAL
                and RANK.get(a.status, 0) < RANK.get(old, 0)
            )
            if regress:
                print(f"kept status '{old}' (refused to regress to '{a.status}'; use --force)")
            else:
                match["status"] = a.status
            for field, val in (("coverage", a.coverage), ("url", a.url),
                               ("notes", a.notes), ("pdf", a.pdf)):
                if val:
                    match[field] = val
            if a.role:
                match["role"] = a.role
            print(f"updated: {match['company']} | {match.get('role','')} | {match['status']}")
        else:
            jobs.insert(0, {
                "id": f"{a.date}-{re.sub(r'[^a-z0-9]+', '-', a.company.lower())}-{len(jobs)}",
                "date": a.date,
                "company": a.company,
                "role": a.role,
                "coverage": a.coverage,
                "status": a.status,
                "url": a.url,
                "notes": a.notes,
                "pdf": a.pdf,
            })
            print(f"added: {a.company} | {a.role} | {a.status}")

        # Atomic write (temp + rename) so an interrupted write can't truncate
        # the file, matching tracker.py's save().
        tmp = QUEUE.with_name(QUEUE.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(QUEUE)
        print(f"job_queue.json now has {len(jobs)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
