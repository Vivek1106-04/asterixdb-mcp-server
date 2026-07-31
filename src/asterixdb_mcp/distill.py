"""Cross-session distillation of episodic query events into memory notes.

The "sleep consolidation" tier of the memory loop. The gateway records one
event per query outcome (``AgentMemory.SessionEvent`` on the cluster, JSONL
buffer offline); distillation mines those events for the signal no single
session can see:

- proven queries: a statement that succeeded against the same dataset in at
  least ``min_sessions`` distinct sessions becomes a learned note carrying the
  statement as its ``source_query``, so revalidation keeps it grounded,
- recurring failures: an error class hit repeatedly against a dataset with no
  recorded success becomes a caution note,
- slow patterns: a statement that keeps succeeding but averages above a
  wall-clock threshold becomes a performance-caution note, so the next session
  restructures instead of rediscovering the cost.

Notes are written through ``run_memory_write`` so they reconcile exactly like
tool-call writes: duplicates no-op, catalog concepts get overlay annotations,
history stays bi-temporal.

Two callers share this module: ``scripts/memory_distill.py`` (manual/cron) and
the HTTP gateway's auto-distill background loop (see ``http_app``), which
makes consolidation zero-effort for non-technical operators.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .cc_client import CCClient
from .config import Settings
from .context_id import make_client_context_id
from .errors import GatewayError
from .memory_store import SESSION_EVENT_DATASET, scope_clause
from .tools.memory_notes import subjects_from_statement
from .tools.memory_write import run_memory_write

MIN_SESSIONS_DEFAULT = 2
MIN_FAILURES_DEFAULT = 3
# Slow-pattern thresholds: a statement must succeed at least this many times and
# average above this wall-clock cost before it is worth a performance caution.
MIN_SLOW_OCCURRENCES_DEFAULT = 3
SLOW_MS_DEFAULT = 5_000.0

EVENTS_QUERY = f"SELECT VALUE e FROM {SESSION_EVENT_DATASET} e WHERE {scope_clause('e')};"


def dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate events (buffer replays can double-record) by identity.

    The event ``id`` is the primary identity; events from old logs without an
    id fall back to (session, ts, statement).
    """
    seen: set[Any] = set()
    unique: list[dict[str, Any]] = []
    for event in events:
        key = event.get("id") or (event.get("session"), event.get("ts"), event.get("statement"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique


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


def slow_patterns(
    events: list[dict[str, Any]], min_occurrences: int, slow_ms: float
) -> list[tuple[str, str]]:
    """(subject, note) for statements that keep succeeding but run slowly.

    Only successful events with a numeric ``elapsed_ms`` count; a statement is
    flagged when it has at least ``min_occurrences`` such runs whose mean cost
    exceeds ``slow_ms``. The note is UNVERIFIED on purpose — it is a heuristic
    from past timings, not a fact a single query proves, so it must not carry a
    source_query.
    """
    timings: dict[tuple[str, str], list[float]] = defaultdict(list)
    for event in events:
        if event.get("outcome") != "success":
            continue
        elapsed = event.get("elapsed_ms")
        if not isinstance(elapsed, (int, float)):
            continue
        statement = str(event.get("statement", ""))
        for subject in subjects_from_statement(statement):
            timings[(subject, statement)].append(float(elapsed))
    distilled = []
    for (subject, statement), samples in sorted(timings.items()):
        if len(samples) < min_occurrences:
            continue
        avg_ms = sum(samples) / len(samples)
        if avg_ms <= slow_ms:
            continue
        note = (
            f"Slow pattern: this query averaged {avg_ms / 1000:.1f}s over "
            f"{len(samples)} runs — consider restructuring before reuse: {statement}"
        )
        distilled.append((subject, note))
    return distilled


async def fetch_cluster_events(client: CCClient, ccid: str) -> list[dict[str, Any]]:
    """Read every recorded session event from the cluster (best-effort)."""
    try:
        envelope = await client.execute_memory_read(EVENTS_QUERY, client_context_id=ccid)
    except GatewayError:
        return []
    return [row for row in envelope.get("results", []) if isinstance(row, dict)]


async def run_distill(
    client: CCClient,
    settings: Settings,
    *,
    min_sessions: int = MIN_SESSIONS_DEFAULT,
    min_failures: int = MIN_FAILURES_DEFAULT,
    min_slow_occurrences: int = MIN_SLOW_OCCURRENCES_DEFAULT,
    slow_ms: float = SLOW_MS_DEFAULT,
) -> dict[str, int]:
    """One in-process distill pass over the cluster's session events.

    Returns a summary counter: events seen, notes distilled, and the write
    actions taken. Failed note writes are counted, never raised — the loop
    calling this must survive any single bad pass.
    """
    ccid = make_client_context_id(settings.agent_session_id, "distill")
    events = dedupe_events(await fetch_cluster_events(client, ccid))
    proven = proven_queries(events, min_sessions)
    cautions = recurring_failures(events, min_failures)
    slow = slow_patterns(events, min_slow_occurrences, slow_ms)

    summary: dict[str, int] = defaultdict(int)
    summary["events"] = len(events)
    for subject, note, source_query in proven:
        await _write_distilled(client, settings, subject, note, summary, source_query=source_query)
    for subject, note in cautions:
        await _write_distilled(client, settings, subject, note, summary)
    for subject, note in slow:
        await _write_distilled(client, settings, subject, note, summary)
    return dict(summary)


async def _write_distilled(
    client: CCClient,
    settings: Settings,
    subject: str,
    note: str,
    summary: dict[str, int],
    *,
    source_query: str | None = None,
) -> None:
    """Persist one distilled note and tally the reconcile action into ``summary``."""
    result = await run_memory_write(
        client,
        settings,
        subject=subject,
        text=note,
        tags=["distilled"],
        source_query=source_query,
    )
    action = "failed" if result.is_error else str(result.structured.get("action"))
    summary[action] += 1
