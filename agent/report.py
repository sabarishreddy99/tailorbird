#!/usr/bin/env python3
"""
report.py - run-log + status file for the resume agent.

Writes two things under agent/runs/:
  - status.json          machine-readable state the tracker UI polls
                         (running/idle, counts, per-role results, log path)
  - <YYYY-MM-DD-HHMM>.md  a human run-log summarizing built / dropped /
                         needs_review with reasons, mirroring the end-of-batch
                         summaries produced in interactive sessions.

Writes are atomic (temp + rename) so the UI never reads a half-written file.
"""

import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "agent" / "runs"
STATUS = RUNS / "status.json"
STAGING = RUNS / "staging"


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def now_id():
    return datetime.now().strftime("%Y-%m-%d-%H%M%S")


def write_status(state, run_id, results=None, started=None, log_path=None):
    results = results or []
    counts = {"total": len(results)}
    for v in ("built", "dropped", "needs_review", "failed"):
        counts[v] = sum(1 for r in results if r.get("outcome") == v)
    totals = {
        "tokens": sum(r.get("tokens") or 0 for r in results),        # fresh tokens
        "seconds": sum(r.get("seconds") or 0 for r in results),
        "cost": round(sum(r.get("cost") or 0 for r in results), 2),
    }
    payload = {
        "state": state,                       # "running" | "idle"
        "run_id": run_id,
        "started": started,
        "finished": datetime.now().isoformat(timespec="seconds") if state == "idle" else None,
        "counts": counts,
        "totals": totals,
        "log": str(log_path.relative_to(ROOT)) if log_path else None,
        "results": [
            {k: r.get(k, "") for k in
             ("company", "role", "url", "outcome", "reason", "coverage",
              "tokens", "seconds", "turns")}
            for r in results
        ],
    }
    _atomic_write(STATUS, json.dumps(payload, indent=2, ensure_ascii=False))
    return payload


def write_runlog(run_id, results):
    """Write the dated markdown run-log; return its path."""
    lines = [f"# Agent run {run_id}", ""]
    counts = {v: sum(1 for r in results if r.get("outcome") == v)
              for v in ("built", "dropped", "needs_review", "failed")}
    tot_tok = sum(r.get("tokens") or 0 for r in results)
    tot_sec = sum(r.get("seconds") or 0 for r in results)
    tot_cost = sum(r.get("cost") or 0 for r in results)
    summary = (f"**{len(results)} processed** — {counts['built']} built · "
               f"{counts['dropped']} dropped · {counts['needs_review']} need review · "
               f"{counts['failed']} failed")
    tel = []
    if tot_cost:
        tel.append(f"${tot_cost:.2f}")
    if tot_tok:
        tel.append(f"{tot_tok // 1000}k new tokens")
    if tot_sec:
        tel.append(f"{tot_sec}s skill time")
    if tel:
        summary += "  ·  **" + " · ".join(tel) + "**"
    lines.append(summary)
    lines.append("")
    lines.append("| Outcome | Company | Role | Coverage | Cost | New tok | Time | Reason |")
    lines.append("|---|---|---|---|---|---|---|---|")
    order = {"built": 0, "needs_review": 1, "dropped": 2, "failed": 3}
    for r in sorted(results, key=lambda x: order.get(x.get("outcome"), 9)):
        icon = {"built": "✅", "dropped": "❌", "needs_review": "⚠️", "failed": "🛑"}.get(
            r.get("outcome"), "")
        reason = (r.get("reason", "") or "").replace("|", "/").replace("\n", " ")[:160]
        cost = f"${r['cost']:.2f}" if r.get("cost") is not None and r.get("cost") != "" else ""
        tok = f"{(r.get('tokens') or 0) // 1000}k" if r.get("tokens") else ""
        sec = f"{r.get('seconds')}s" if r.get("seconds") else ""
        lines.append(
            f"| {icon} {r.get('outcome','')} | {r.get('company','')} | "
            f"{r.get('role','')} | {r.get('coverage','')} | {cost} | {tok} | {sec} | {reason} |")
    lines.append("")
    log_path = RUNS / f"{run_id}.md"
    _atomic_write(log_path, "\n".join(lines))
    return log_path


def read_status():
    if STATUS.exists():
        try:
            return json.loads(STATUS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"state": "idle", "run_id": None, "counts": {}, "results": [], "log": None}


if __name__ == "__main__":
    rid = now_id()
    demo = [
        {"company": "Duolingo", "role": "Sr SWE Backend", "outcome": "built", "coverage": "~85%",
         "reason": "clear fit", "url": "u"},
        {"company": "New Relic", "role": "SWE-ELB", "outcome": "dropped",
         "reason": "sponsorship not provided", "url": "u"},
        {"company": "Pipe17", "role": "Jr SWE", "outcome": "needs_review",
         "reason": "entry level, no comp stated", "url": "u"},
    ]
    write_status("idle", rid, demo, started=datetime.now().isoformat(), log_path=write_runlog(rid, demo))
    print("wrote", STATUS)
    print((RUNS / f"{rid}.md").read_text())
