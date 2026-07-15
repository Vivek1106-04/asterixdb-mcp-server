"""Distill session logs into the agentic-memory store.

The offline half of automatic capture. The gateway appends one JSONL event per
query outcome to its session log directory (config: session_log_dir); this
script reads every session's log and distills the cross-session signal the
inline capture cannot see:

- proven queries: a statement that succeeded against the same dataset in at
  least --min-sessions distinct sessions becomes a learned note carrying the
  statement as its source_query, so revalidation keeps it grounded,
- recurring failures: an error class hit repeatedly against a dataset without
  a recorded success becomes a caution note.

Notes are reconciled exactly like memory_write tool calls (duplicates no-op,
catalog concepts get overlay annotations, history stays bi-temporal) by
reusing the same pure reconcile function and write statements.

Usage:
    python scripts/memory_distill.py --logs-dir /path/to/session-logs \\
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

from asterixdb_mcp.tools.memory_notes import subjects_from_statement  # noqa: E402
from asterixdb_mcp.tools.memory_write import (  # noqa: E402
    _CURRENT_QUERY,
    _INSERT,
    _UPSERT,
    _reconcile,
)

MIN_SESSIONS_DEFAULT = 2
MIN_FAILURES_DEFAULT = 3


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


def proven_queries(events: list[dict[str, Any]], min_sessions: int) -> list[tuple[str, str, str]]:
    """(subject, note, source_query) for statements proven across sessions."""
    sessions_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for event in events:
        if event.get("outcome") != "success":
            continue
        statement = str(event.get("statement", ""))
        for subject in subjects_from_statement(statement):
            sessions_by_key[(subject, statement)].add(str(event.get("session", "")))
    distilled = []
    for (subject, statement), sessions in sorted(sessions_by_key.items()):
        if len(sessions) >= min_sessions:
            note = f"Proven query, used successfully in {len(sessions)} sessions: {statement}"
            distilled.append((subject, note, statement))
    return distilled


def recurring_failures(events: list[dict[str, Any]], min_failures: int) -> list[tuple[str, str]]:
    """(subject, note) for error classes that repeat with no recorded success."""
    failures: dict[tuple[str, str], int] = defaultdict(int)
    resolved: set[str] = set()
    for event in events:
        statement = str(event.get("statement", ""))
        for subject in subjects_from_statement(statement):
            if event.get("outcome") == "success":
                resolved.add(subject)
            else:
                failures[(subject, str(event.get("error", "")))] += 1
    distilled = []
    for (subject, error), count in sorted(failures.items()):
        if count >= min_failures and subject not in resolved:
            note = f"Caution: queries on this dataset failed {count} times with {error}."
            distilled.append((subject, note))
    return distilled


def write_note(cc: str, subject: str, note: str, source_query: str | None = None) -> str:
    """Persist one distilled note through the memory_write reconcile path."""
    envelope = execute(cc, _CURRENT_QUERY.replace("$subject", f'"{subject}"'))
    rows = [r for r in envelope.get("results", []) if isinstance(r, dict)]
    existing = rows[0] if rows else None
    now = datetime.now(timezone.utc).isoformat()
    action, row = _reconcile(existing, subject, note, now, None, ["distilled"], source_query)
    if action == "unchanged":
        return action
    if existing is not None:
        execute(cc, _UPSERT.replace("$row", json.dumps({**existing, "valid_to": now})))
    execute(cc, _INSERT.replace("$row", json.dumps(row)))
    return action


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--logs-dir", required=True, help="Directory of session JSONL logs")
    parser.add_argument("--cc", default="http://localhost:19002", help="Cluster controller URL")
    parser.add_argument("--min-sessions", type=int, default=MIN_SESSIONS_DEFAULT)
    parser.add_argument("--min-failures", type=int, default=MIN_FAILURES_DEFAULT)
    parser.add_argument("--dry-run", action="store_true", help="Report, write nothing")
    args = parser.parse_args()

    events = load_events(Path(args.logs_dir))
    proven = proven_queries(events, args.min_sessions)
    cautions = recurring_failures(events, args.min_failures)

    actions: dict[str, int] = defaultdict(int)
    if not args.dry_run:
        for subject, note, source_query in proven:
            actions[write_note(args.cc, subject, note, source_query)] += 1
        for subject, note in cautions:
            actions[write_note(args.cc, subject, note)] += 1
    summary = "dry-run" if args.dry_run else " | ".join(
        f"{count} {action}" for action, count in sorted(actions.items())
    ) or "nothing to write"
    print(
        f"memory_distill: {len(events)} events | {len(proven)} proven | "
        f"{len(cautions)} cautions | {summary}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
