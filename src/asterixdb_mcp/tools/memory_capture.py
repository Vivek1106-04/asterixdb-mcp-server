"""Deterministic capture of learned knowledge into the memory store.

The gateway watches query outcomes itself: when a statement against a dataset
fails and a later statement against that same dataset succeeds in the same
session, the pair is distilled into a learned note on the dataset's concept.
The note is written through the same reconcile path as the memory_write tool,
so duplicates no-op, catalog concepts get overlay annotations, and history
stays bi-temporal. No model involvement — capture happens whether or not the
client ever calls memory_write.

When a session log directory is configured, every query outcome is also
appended as one JSONL event, giving scripts/memory_distill.py the raw
episodic record to distill cross-session knowledge from offline.

Capture is best-effort throughout: a failed note write or an unwritable log
never surfaces to the caller.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from .memory_notes import subjects_from_statement
from .memory_write import run_memory_write

MAX_CAPTURED_STATEMENT_LEN = 300


@dataclass
class _PendingFailure:
    error: str
    statement: str


class CaptureState:
    """Per-session record of failed statements awaiting a working form."""

    def __init__(self) -> None:
        self._failures: dict[str, _PendingFailure] = {}

    def record(self, statement: str, result_error: str | None) -> list[tuple[str, str]]:
        """Track one query outcome; return (subject, note) pairs ready to persist."""
        subjects = subjects_from_statement(statement)
        if result_error is not None:
            for subject in subjects:
                self._failures[subject] = _PendingFailure(result_error, statement)
            return []
        captured: list[tuple[str, str]] = []
        for subject in subjects:
            pending = self._failures.pop(subject, None)
            if pending is not None and pending.statement.strip() != statement.strip():
                captured.append((subject, _fix_note(pending, statement)))
        return captured


async def capture_query_outcome(
    client: CCClient,
    settings: Settings,
    capture: CaptureState,
    *,
    statement: str,
    result_error: str | None,
) -> None:
    """Feed one query outcome through capture and persist any distilled notes."""
    _append_session_event(settings, statement, result_error)
    if not settings.memory_write_enabled:
        return
    for subject, note in capture.record(statement, result_error):
        # run_memory_write returns an error ToolResult rather than raising;
        # capture never lets a failed note write surface to the caller.
        await run_memory_write(client, settings, subject=subject, text=note)


def _fix_note(pending: _PendingFailure, statement: str) -> str:
    return (
        f"A query on this dataset failed ({pending.error}): "
        f"{_trim(pending.statement)} | working form: {_trim(statement)}"
    )


def _trim(statement: str) -> str:
    flat = " ".join(statement.split())
    if len(flat) <= MAX_CAPTURED_STATEMENT_LEN:
        return flat
    return flat[:MAX_CAPTURED_STATEMENT_LEN] + "..."


def _append_session_event(settings: Settings, statement: str, result_error: str | None) -> None:
    if not settings.session_log_dir:
        return
    event: dict[str, Any] = {
        "ts": time.time(),
        "session": settings.agent_session_id,
        "statement": statement,
        "outcome": "error" if result_error is not None else "success",
    }
    if result_error is not None:
        event["error"] = result_error
    try:
        log_dir = Path(settings.session_log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{settings.agent_session_id}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        return
