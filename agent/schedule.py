#!/usr/bin/env python3
"""
schedule.py - manage the resume agent's macOS launchd schedule.

Generates / installs / removes ~/Library/LaunchAgents/com.tailorbird.scheduler.plist
so the agent runs on a calendar schedule with no Claude window open. Driven by
the tracker UI's Schedule panel (/api/schedule) and usable from the CLI:

    python3 agent/schedule.py status
    python3 agent/schedule.py enable --times 09:00,13:00,18:00 --concurrency 3
    python3 agent/schedule.py disable

Uses the modern `launchctl bootstrap` / `bootout gui/$UID` interface.
"""

import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.tailorbird.scheduler"
ROOT = Path(__file__).resolve().parent.parent
RUN_PY = ROOT / "agent" / "run.py"
LOG = ROOT / "agent" / "runs" / "launchd.log"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
UID = os.getuid()

DEFAULT_TIMES = ["09:00", "13:00", "18:00"]
DEFAULT_CONCURRENCY = 3


def _times_to_intervals(times):
    out = []
    for t in times:
        hh, mm = t.split(":")
        out.append({"Hour": int(hh), "Minute": int(mm)})
    return out


def _intervals_to_times(intervals):
    if isinstance(intervals, dict):
        intervals = [intervals]
    return [f"{i.get('Hour', 0):02d}:{i.get('Minute', 0):02d}" for i in intervals]


def build_plist(times, concurrency):
    return {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable, str(RUN_PY), "--once", "--concurrency", str(concurrency),
        ],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": _times_to_intervals(times),
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "RunAtLoad": False,
        "ProcessType": "Background",
    }


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def is_loaded():
    return _launchctl("print", f"gui/{UID}/{LABEL}").returncode == 0


def enable(times=None, concurrency=None):
    times = times or DEFAULT_TIMES
    concurrency = concurrency or DEFAULT_CONCURRENCY
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST, "wb") as fh:
        plistlib.dump(build_plist(times, concurrency), fh)
    # Reload cleanly: bootout if already loaded, then bootstrap.
    if is_loaded():
        _launchctl("bootout", f"gui/{UID}/{LABEL}")
    r = _launchctl("bootstrap", f"gui/{UID}", str(PLIST))
    ok = r.returncode == 0 or is_loaded()
    return {"ok": ok, "detail": (r.stderr or r.stdout).strip(), **status()}


def disable():
    r = _launchctl("bootout", f"gui/{UID}/{LABEL}")
    # Keep the plist file? Remove it so status reflects "not installed".
    if PLIST.exists():
        PLIST.unlink()
    return {"ok": r.returncode == 0 or not is_loaded(), "detail": (r.stderr or r.stdout).strip(), **status()}


def status():
    installed = PLIST.exists()
    times, concurrency = DEFAULT_TIMES, DEFAULT_CONCURRENCY
    if installed:
        try:
            with open(PLIST, "rb") as fh:
                data = plistlib.load(fh)
            times = _intervals_to_times(data.get("StartCalendarInterval", []))
            args = data.get("ProgramArguments", [])
            if "--concurrency" in args:
                concurrency = int(args[args.index("--concurrency") + 1])
        except Exception:
            pass
    return {
        "installed": installed,
        "loaded": is_loaded(),
        "times": times,
        "concurrency": concurrency,
        "plist": str(PLIST),
        "label": LABEL,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["status", "enable", "disable"])
    p.add_argument("--times", default=",".join(DEFAULT_TIMES),
                   help="comma-separated HH:MM run times")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    a = p.parse_args()
    if a.action == "status":
        out = status()
    elif a.action == "enable":
        out = enable([t.strip() for t in a.times.split(",") if t.strip()], a.concurrency)
    else:
        out = disable()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
