"""Append-only run metrics.

Mostly counts and token usage. This file answers "is the gate set right, and
what is this costing".

**It is NOT safe to paste into an issue.** It used to say it was, and that held
until `deferred_questions` and `unfinished_questions` were added so a task
dropped by the cap or lost to a failure could be re-raised rather than vanish.
Those are model-restated questions derived from the conversation -- exactly the
content this file had promised to exclude. Keeping them is the better trade, a
silently discarded question being worse, but the promise has to change with the
data rather than outlive it.

The file is gitignored and local. Read it; do not share it.

The two numbers that matter most, per .claude/skills/claude-cascade, are
tasks-per-window and searches-per-task: their product is essentially the whole
cost model. The stage-2.5 resolve rate decides whether the cheap pass is
earning its place or should be switched off.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def record_run(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **payload}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def summarise(path: Path, runs: int = 30) -> dict[str, Any]:
    """Aggregate recent runs, to answer the questions the metrics exist for."""
    if not path.exists():
        return {}
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    recent = lines[-runs:]
    if not recent:
        return {}

    cheap = sum(r.get("cheap_attempted", 0) for r in recent)
    resolved = sum(r.get("cheap_resolved", 0) for r in recent)
    escalated = sum(r.get("escalated", 0) for r in recent)
    return {
        "runs": len(recent),
        "messages": sum(r.get("messages", 0) for r in recent),
        "accepted": sum(r.get("accepted", 0) for r in recent),
        "deferred_over_cap": sum(r.get("deferred_over_cap", 0) for r in recent),
        "escalated": escalated,
        # Below ~0.30 the cheap pass costs more than it saves, because an
        # escalated task pays twice. That is the number that decides whether
        # stage 2.5 stays switched on.
        "cheap_resolve_rate": round(resolved / cheap, 3) if cheap else None,
        "input_tokens": sum(r.get("input_tokens", 0) for r in recent),
        "output_tokens": sum(r.get("output_tokens", 0) for r in recent),
        "searches": sum(r.get("searches", 0) for r in recent),
    }
