#!/usr/bin/env python3
"""
session.py - state and freshness of the primed base session.

Authoring reuses ONE primed session for a whole queue: the skill and the
role-invariant candidate context are loaded once, and every role runs as a
`--resume <base> --fork-session` child, so that prefix is a cheap cache READ
per role instead of being re-created each time.

That reuse is only safe while the primed session still reflects reality. This
module answers two questions the UI needs:

  1. Is the primed session STALE?  It is if any source it was built from has
     changed (the skill files, or the candidate context sliced out of
     LIBRARY.md), or if it was primed for a different model/effort - prompt
     caches are model-scoped, so switching model without re-priming throws the
     cached prefix away silently.

  2. Is its cache still WARM?  Prompt-cache entries expire (the CLI writes
     1-hour ephemeral entries). Past that, the next run re-creates the prefix at
     full price instead of reading it at ~0.1x. That is not a correctness
     problem, just a cost one, so it is reported separately from staleness.

Re-priming happens in a fresh subprocess, so it necessarily re-reads every
source from disk - there is no in-process cache to bust.
"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = ROOT / "agent" / "runs" / "session.json"
SKILL_INSTALL = Path.home() / ".claude" / "skills" / "resume-tailoring"
RESUMES = ROOT / "resumes"
# MCP servers attached to the base session. Deliberately NOT a repo-root
# .mcp.json: that would be auto-discovered by interactive Claude Code sessions
# in this repo too, adding the tool definitions to every one of them. The agent
# passes this explicitly via --mcp-config.
MCP_CONFIG = ROOT / "agent" / "mcp.json"

# Only the files the skill actually loads into a session. The install tree also
# carries README/MARKETPLACE/docs (~43k tokens of markdown that never enters a
# session); hashing those would force re-primes for unrelated doc edits.
SKILL_LOADED_FILES = (
    "skills/resume-tailoring/SKILL.md", "multi-job-workflow.md",
    "matching-strategies.md", "branching-questions.md", "research-prompts.md",
)

# The CLI writes 1-hour ephemeral cache entries; past that the prefix is
# re-created rather than read.
CACHE_TTL_MIN = 60


def _digest(chunks):
    h = hashlib.sha256()
    for c in chunks:
        h.update(c if isinstance(c, bytes) else str(c).encode())
    return h.hexdigest()[:16]


def _skill_files():
    return [SKILL_INSTALL / rel for rel in SKILL_LOADED_FILES]


def _resume_corpus():
    """Every base resume the archetype scorer reads. These feed the PER-ROLE kit
    rather than the primed prefix, so a change here does not require a re-prime -
    it is picked up automatically on the next run - but the UI still surfaces it
    as 'new data' because that is what the user actually means by it."""
    out = [p for p in RESUMES.glob("*.md") if "Report" not in p.name]
    for d in sorted(RESUMES.iterdir()) if RESUMES.exists() else []:
        if d.is_dir() and not d.name.startswith("_"):
            out += [p for p in sorted(d.glob("*.md")) if "Report" not in p.name]
    return out


def _source(name, paths, affects_prime, note):
    newest, total = 0, 0
    chunks = []
    for p in paths:
        try:
            st = p.stat()
            newest = max(newest, int(st.st_mtime))
            total += st.st_size
            chunks.append(p.name.encode())
            chunks.append(p.read_bytes())
        except Exception:
            continue
    return {"name": name, "files": len(paths), "bytes": total,
            "newest": newest, "hash": _digest(chunks),
            "affects_prime": affects_prime, "note": note}


def sources():
    """Every local source the agent reads, with a content hash each."""
    import library  # local import: keeps this module importable from the UI
    out = [
        _source("skill", _skill_files(), True,
                "the /resume-tailoring skill files loaded into the session"),
        {"name": "library", "files": 1,
         "bytes": len(library.build_prime_context()),
         "newest": int(os.path.getmtime(library.LIBRARY)) if library.LIBRARY.exists() else 0,
         "hash": _digest([library.build_prime_context()]),
         "affects_prime": True,
         "note": "candidate facts, boundaries and style rules sliced from LIBRARY.md"},
    ]
    corpus = _resume_corpus()
    out.append(_source("resumes", corpus, False,
                       "base resumes the archetype scorer ranks; picked up on the "
                       "next run without re-priming"))
    # MCP tool definitions render at the very front of the prompt, so a change
    # here invalidates the cached prefix outright - it must force a re-prime.
    out.append(_source("mcp", [MCP_CONFIG] if MCP_CONFIG.exists() else [], True,
                       "MCP servers attached to the base session (" + ", ".join(mcp_servers())
                       + ")" if MCP_CONFIG.exists() else "no MCP servers attached"))
    return out


def mcp_servers():
    """Names of the MCP servers wired into the base session."""
    try:
        return sorted((json.loads(MCP_CONFIG.read_text()).get("mcpServers") or {}).keys())
    except Exception:
        return []


def fingerprint(srcs=None):
    """Combined hash of everything baked into the primed session."""
    srcs = srcs if srcs is not None else sources()
    return _digest([s["hash"] for s in srcs if s["affects_prime"]])


def read():
    try:
        return json.loads(SESSION_FILE.read_text())
    except Exception:
        return {}


def write(session_id, model, effort, srcs=None):
    srcs = srcs if srcs is not None else sources()
    now = datetime.now().isoformat(timespec="seconds")
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps({
        "session_id": session_id,
        "skill_hash": fingerprint(srcs),
        "model": model,
        "effort": effort or "default",
        "primed_at": now,
        "last_used_at": now,
        "uses": 0,
        "sources": {s["name"]: s["hash"] for s in srcs},
    }, indent=2))


def touch():
    """Record that a role just used the primed session (drives the cache-warmth
    indicator and the 'roles served' count)."""
    st = read()
    if not st:
        return
    st["last_used_at"] = datetime.now().isoformat(timespec="seconds")
    st["uses"] = int(st.get("uses", 0)) + 1
    try:
        SESSION_FILE.write_text(json.dumps(st, indent=2))
    except Exception:
        pass


def invalidate():
    """Drop the primed session so the next run rebuilds it from fresh sources."""
    try:
        SESSION_FILE.unlink()
        return True
    except FileNotFoundError:
        return False


def _age_min(iso):
    try:
        return max(0, int((datetime.now() - datetime.fromisoformat(iso)).total_seconds() // 60))
    except Exception:
        return None


def status(model=None, effort=None):
    """Everything the UI needs to decide whether to show a re-prime prompt."""
    st, srcs = read(), sources()
    want = fingerprint(srcs)
    stale, reasons = False, []

    if not st.get("session_id"):
        stale = True
        reasons.append("No primed session yet - the first run will build one.")
    else:
        if st.get("skill_hash") != want:
            stale = True
            changed = [s["name"] for s in srcs if s["affects_prime"]
                       and (st.get("sources") or {}).get(s["name"]) != s["hash"]]
            reasons.append(
                "Source data changed since priming"
                + (f" ({', '.join(changed)})" if changed else "")
                + " - the session is holding an outdated copy.")
        if model and st.get("model") and st["model"] != model:
            stale = True
            reasons.append(
                f"Primed for model '{st['model']}' but '{model}' is selected. "
                "Prompt caches are model-scoped, so this must be re-primed.")
        if effort is not None and st.get("effort", "default") != (effort or "default"):
            stale = True
            reasons.append(
                f"Primed at effort '{st.get('effort', 'default')}' but "
                f"'{effort or 'default'}' is selected.")

    # Resume corpus drift is informational: it feeds the per-role kit, not the
    # primed prefix, so it needs no re-prime.
    corpus = next((s for s in srcs if s["name"] == "resumes"), None)
    corpus_changed = bool(st and corpus
                          and (st.get("sources") or {}).get("resumes")
                          and st["sources"]["resumes"] != corpus["hash"])

    idle = _age_min(st.get("last_used_at") or st.get("primed_at") or "")
    cache_warm = idle is not None and idle < CACHE_TTL_MIN
    return {
        "session_id": st.get("session_id"),
        "short_id": (st.get("session_id") or "")[:8],
        "primed_at": st.get("primed_at"),
        "last_used_at": st.get("last_used_at"),
        "model": st.get("model"),
        "effort": st.get("effort", "default"),
        "uses": st.get("uses", 0),
        "age_min": _age_min(st.get("primed_at") or ""),
        "idle_min": idle,
        "cache_warm": cache_warm,
        "cache_note": (
            "Prefix cache is warm - the next role reads it at ~0.1x."
            if cache_warm else
            f"Idle over {CACHE_TTL_MIN}m, so the cached prefix has expired. The "
            "next role re-creates it at full price once, then it is warm again. "
            "Re-priming does not avoid this."),
        "stale": stale,
        "reasons": reasons,
        "corpus_changed": corpus_changed,
        "sources": srcs,
    }


if __name__ == "__main__":
    print(json.dumps(status(), indent=2, default=str))
