"""Deterministic capture of learned knowledge into the memory store.

The gateway watches query outcomes itself: when a statement against a dataset
fails and a later statement against that same dataset succeeds in the same
session, the pair is distilled into a learned note on the dataset's concept.
The note is written through the same reconcile path as the memory_write tool,
so duplicates no-op, catalog concepts get overlay annotations, and history
stays bi-temporal. No model involvement — capture happens whether or not the
client ever calls memory_write.

Failure means more than a raised error: a query that compiles, runs, and
returns 0 rows WITH type-mismatch warnings silently failed (the classic
UNNEST-a-string miss), so ``capture_error_signal`` treats that as a failure
signal too.

Every query outcome is also recorded as one episodic event for offline
distillation (scripts/memory_distill.py and the gateway's own auto-distill):
into ``AgentMemory.SessionEvent`` on the cluster when memory writes are
enabled, with the session-log JSONL file as the offline buffer — events that
could not reach the cluster are appended there and flushed on the next
successful cluster write.

Capture is best-effort throughout: a failed note write, an unreachable
cluster, or an unwritable log never surfaces to the caller.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import GatewayError
from . import ToolResult
from .memory_notes import subjects_from_statement
from .memory_write import run_memory_write

MAX_CAPTURED_STATEMENT_LEN = 300
MAX_WARNING_LEN = 200

from ..memory_store import SESSION_EVENT_DATASET  # noqa: E402  re-exported

_EVENT_INSERT = f"INSERT INTO {SESSION_EVENT_DATASET} ([$row]);"

# CC metrics report durations as strings like "377.644875ms" or "2.233s".
_DURATION_RE = re.compile(r"^([\d.]+)(ns|µs|us|ms|s)$")
_DURATION_TO_MS = {"ns": 1e-6, "µs": 1e-3, "us": 1e-3, "ms": 1.0, "s": 1000.0}


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


def capture_error_signal(result: ToolResult) -> str | None:
    """The failure signal of a query result, or None when it truly succeeded.

    A raised error carries its classified errorType. A "successful" result
    with zero rows AND compiler warnings is a silent semantic miss — the query
    ran but computed nothing — and is surfaced as a SEMANTIC_WARNING signal so
    capture can pair it with the later working form.
    """
    structured = result.structured or {}
    if result.is_error:
        return str(structured.get("errorType")) if structured.get("errorType") else None
    warnings = structured.get("warnings")
    if structured.get("rowsReturned") == 0 and warnings:
        first = warnings[0]
        msg = str(first.get("msg", "")) if isinstance(first, dict) else str(first)
        return f"SEMANTIC_WARNING: {msg[:MAX_WARNING_LEN]}"
    return None


async def capture_query_outcome(
    client: CCClient,
    settings: Settings,
    capture: CaptureState,
    *,
    statement: str,
    result_error: str | None,
    client_name: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> None:
    """Feed one query outcome through capture and persist any distilled notes."""
    if not settings.memory_enabled:
        return
    await _record_session_event(
        client, settings, statement, result_error, client_name=client_name, metrics=metrics
    )
    if not settings.memory_write_enabled:
        return
    for subject, note in capture.record(statement, result_error):
        # run_memory_write returns an error ToolResult rather than raising;
        # capture never lets a failed note write surface to the caller. The
        # working statement doubles as grounding evidence for revalidation.
        await run_memory_write(
            client,
            settings,
            subject=subject,
            text=note,
            source_query=statement,
            author=client_name,
        )


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


# episodic session events


def _build_event(
    settings: Settings,
    statement: str,
    result_error: str | None,
    *,
    client_name: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": f"{settings.agent_session_id}@{time.time()}-{uuid.uuid4().hex[:8]}",
        "ts": time.time(),
        "session": settings.agent_session_id,
        "statement": statement,
        "outcome": "error" if result_error is not None else "success",
    }
    if result_error is not None:
        event["error"] = result_error
    if client_name:
        event["client"] = client_name
    elapsed = _elapsed_ms(metrics)
    if elapsed is not None:
        event["elapsed_ms"] = elapsed
    if metrics and isinstance(metrics.get("processedObjects"), int):
        event["processed_objects"] = metrics["processedObjects"]
    return event


def _elapsed_ms(metrics: dict[str, Any] | None) -> float | None:
    """Parse the CC's elapsedTime duration string into milliseconds, if present."""
    if not metrics:
        return None
    match = _DURATION_RE.match(str(metrics.get("elapsedTime", "")))
    if match is None:
        return None
    return round(float(match.group(1)) * _DURATION_TO_MS[match.group(2)], 3)


async def _record_session_event(
    client: CCClient,
    settings: Settings,
    statement: str,
    result_error: str | None,
    *,
    client_name: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> None:
    """Record one episodic event: cluster first, JSONL buffer as the fallback."""
    event = _build_event(
        settings, statement, result_error, client_name=client_name, metrics=metrics
    )
    if settings.memory_write_enabled:
        try:
            await _insert_event(client, settings, event)
            await _flush_buffered_events(client, settings)
            return
        except GatewayError:
            pass
    _append_jsonl_event(settings, event)


async def _insert_event(client: CCClient, settings: Settings, event: dict[str, Any]) -> None:
    ccid = make_client_context_id(settings.agent_session_id, "session_event")
    await client.execute_memory_write(
        _EVENT_INSERT, client_context_id=ccid, statement_parameters={"row": event}
    )


async def _flush_buffered_events(client: CCClient, settings: Settings) -> None:
    """Replay events buffered while the cluster was unreachable, then drop the buffers.

    Every ``*.jsonl`` file in the log directory is replayed — session ids are
    per-process, so a crashed session's buffer has a different name than ours
    and would otherwise be stranded forever. A failure mid-flush leaves the
    remaining lines in place for the next attempt; duplicate replays are
    tolerable because distillation deduplicates by event id.
    """
    if not settings.session_log_dir:
        return
    directory = Path(settings.session_log_dir)
    try:
        buffers = sorted(directory.glob("*.jsonl"))
    except OSError:
        return
    for path in buffers:
        await _flush_one_buffer(client, settings, path)


async def _flush_one_buffer(client: CCClient, settings: Settings, path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event.setdefault("id", f"{event.get('session', 'buffered')}@{uuid.uuid4().hex[:8]}")
        await _insert_event(client, settings, event)
    try:
        path.unlink()
    except OSError:
        return


def _append_jsonl_event(settings: Settings, event: dict[str, Any]) -> None:
    path = _buffer_path(settings)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        return


def _buffer_path(settings: Settings) -> Path | None:
    if not settings.session_log_dir:
        return None
    return Path(settings.session_log_dir) / f"{settings.agent_session_id}.jsonl"
