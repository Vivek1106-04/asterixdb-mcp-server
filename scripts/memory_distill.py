"""Distill session events into the agentic-memory store (manual/cron entry).

The offline half of automatic capture. The gateway records one event per query
outcome — into ``AgentMemory.SessionEvent`` on the cluster when memory writes
are enabled, with per-session JSONL files as the offline buffer. This script
gathers events from BOTH sources (either may be empty), deduplicates them, and
distills the cross-session signal the inline capture cannot see:

- proven queries: a statement that succeeded against the same dataset in at
  least --min-sessions distinct sessions becomes a learned note carrying the
  statement as its source_query, so revalidation keeps it grounded,
- recurring failures: an error class hit repeatedly against a dataset without
  a recorded success becomes a caution note.

Notes are reconciled exactly like memory_write tool calls (duplicates no-op,
catalog concepts get overlay annotations, history stays bi-temporal) by
reusing the same pure reconcile function and write statements.

The HTTP gateway can run the same pass automatically on an interval
(ASTERIXDB_MCP_DISTILL_INTERVAL_S); this script remains the manual override
and the path for pure-stdio deployments.

Usage:
    python scripts/memory_distill.py [--logs-dir /path/to/session-logs] \\
        [--cc http://localhost:19002] [--min-sessions 2] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from okf_refresh import execute

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from asterixdb_mcp.distill import (  # noqa: E402
    EVENTS_QUERY,
    MIN_FAILURES_DEFAULT,
    MIN_SESSIONS_DEFAULT,
    dedupe_events,
    proven_queries,
    recurring_failures,
)
from asterixdb_mcp.tools.memory_write import (  # noqa: E402
    _CURRENT_QUERY,
    _INSERT,
    _UPSERT,
    _reconcile,
)


def load_events(logs_dir: Path) -> list[dict[str, Any]]:
    """Read every session's JSONL events; malformed lines are skipped."""
    events: list[dict[str, Any]] = []
    for path in sorted(logs_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def load_cluster_events(cc: str) -> list[dict[str, Any]]:
    """Read events recorded in AgentMemory.SessionEvent; empty when unreachable."""
    try:
        envelope = execute(cc, EVENTS_QUERY)
    except Exception:
        return []
    return [row for row in envelope.get("results", []) if isinstance(row, dict)]


def write_note(cc: str, subject: str, note: str, source_query: str | None = None) -> str:
    """Persist one distilled note through the memory_write reconcile path."""
    envelope = execute(cc, _CURRENT_QUERY.replace("$subject", f'"{subject}"'))
    rows = [r for r in envelope.get("results", []) if isinstance(r, dict)]
    existing = rows[0] if rows else None
    now = datetime.now(timezone.utc).isoformat()
    action, row, _retired = _reconcile(
        existing, subject, note, now, None, ["distilled"], source_query
    )
    if action == "unchanged":
        return action
    if existing is not None:
        execute(cc, _UPSERT.replace("$row", json.dumps({**existing, "valid_to": now})))
    execute(cc, _INSERT.replace("$row", json.dumps(row)))
    return action


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--logs-dir",
        default=None,
        help="Directory of session JSONL logs (optional; cluster events are always read)",
    )
    parser.add_argument("--cc", default="http://localhost:19002", help="Cluster controller URL")
    parser.add_argument("--min-sessions", type=int, default=MIN_SESSIONS_DEFAULT)
    parser.add_argument("--min-failures", type=int, default=MIN_FAILURES_DEFAULT)
    parser.add_argument("--dry-run", action="store_true", help="Report, write nothing")
    args = parser.parse_args()

    events = load_cluster_events(args.cc)
    if args.logs_dir:
        events += load_events(Path(args.logs_dir))
    events = dedupe_events(events)
    proven = proven_queries(events, args.min_sessions)
    cautions = recurring_failures(events, args.min_failures)

    actions: dict[str, int] = defaultdict(int)
    if not args.dry_run:
        for subject, note, source_query in proven:
            actions[write_note(args.cc, subject, note, source_query)] += 1
        for subject, note in cautions:
            actions[write_note(args.cc, subject, note)] += 1
    summary = (
        "dry-run"
        if args.dry_run
        else " | ".join(f"{count} {action}" for action, count in sorted(actions.items()))
        or "nothing to write"
    )
    print(
        f"memory_distill: {len(events)} events | {len(proven)} proven | "
        f"{len(cautions)} cautions | {summary}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
