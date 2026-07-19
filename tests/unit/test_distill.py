"""Unit tests for in-process cross-session distillation (asterixdb_mcp.distill)."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.distill import (
    dedupe_events,
    fetch_cluster_events,
    proven_queries,
    recurring_failures,
    run_distill,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio

STMT = "SELECT VALUE c FROM ShopDV.customers c LIMIT 5;"
FAIL_STMT = "SELECT a FROM DV.a x;"


def test_dedupe_by_id_then_by_composite_key() -> None:
    events = [
        {"id": "e1", "statement": STMT},
        {"id": "e1", "statement": STMT},
        {"session": "a", "ts": 1.0, "statement": STMT},
        {"session": "a", "ts": 1.0, "statement": STMT},
        {"session": "a", "ts": 2.0, "statement": STMT},
    ]
    assert len(dedupe_events(events)) == 3


def test_proven_queries_requires_distinct_sessions() -> None:
    events = [
        {"outcome": "success", "statement": STMT, "session": "a"},
        {"outcome": "success", "statement": STMT, "session": "a"},
        {"outcome": "success", "statement": STMT, "session": "b"},
        {"outcome": "error", "statement": STMT, "session": "c"},
    ]
    assert proven_queries(events, min_sessions=2) == [
        ("ShopDV.customers", f"Proven query, used successfully in 2 sessions: {STMT}", STMT)
    ]
    assert proven_queries(events, min_sessions=3) == []


def test_recurring_failures_excludes_resolved_subjects() -> None:
    fail = {"outcome": "error", "statement": FAIL_STMT, "error": "QUERY_ERROR"}
    ok = {"outcome": "success", "statement": FAIL_STMT}
    assert recurring_failures([fail, fail, fail], min_failures=3) == [
        ("DV.a", "Caution: queries on this dataset failed 3 times with QUERY_ERROR.")
    ]
    assert recurring_failures([fail, fail, fail, ok], min_failures=3) == []


async def test_fetch_cluster_events_degrades_to_empty(settings: Settings) -> None:
    cap = make_capturing_cc(
        settings,
        response_json={"status": "fatal", "errors": [{"code": 1, "msg": "CC down"}]},
        status_code=500,
    )
    assert await fetch_cluster_events(cap.client, "ccid") == []


async def test_run_distill_writes_proven_and_caution_notes(settings: Settings) -> None:
    settings = settings.model_copy(update={"memory_write_enabled": True})
    events = [
        {"id": "1", "outcome": "success", "statement": STMT, "session": "a"},
        {"id": "2", "outcome": "success", "statement": STMT, "session": "b"},
        {"id": "3", "outcome": "error", "statement": FAIL_STMT, "error": "E", "session": "a"},
        {"id": "4", "outcome": "error", "statement": FAIL_STMT, "error": "E", "session": "b"},
        {"id": "5", "outcome": "error", "statement": FAIL_STMT, "error": "E", "session": "c"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        rows = events if "SessionEvent" in stmt and stmt.startswith("SELECT") else []
        return httpx.Response(200, json={"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    summary = await run_distill(cap.client, settings)

    assert summary["events"] == 5
    # one proven note + one caution note, both freshly created
    assert summary["created"] == 2
    statements = [parse_qs(r.content.decode())["statement"][0] for r in cap.requests]
    assert sum(s.startswith("INSERT INTO AgentMemory.Memory") for s in statements) == 2


async def test_run_distill_counts_failed_writes_without_raising(settings: Settings) -> None:
    # memory writes disabled: note writes return error ToolResults, never raise
    events = [
        {"id": "1", "outcome": "success", "statement": STMT, "session": "a"},
        {"id": "2", "outcome": "success", "statement": STMT, "session": "b"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        rows = events if "SessionEvent" in stmt else []
        return httpx.Response(200, json={"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    summary = await run_distill(cap.client, settings)
    assert summary["failed"] == 1
