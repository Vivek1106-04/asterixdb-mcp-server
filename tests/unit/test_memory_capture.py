"""Unit tests for deterministic error->fix capture and session logging."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.tools.memory_capture import (
    MAX_CAPTURED_STATEMENT_LEN,
    CaptureState,
    _trim,
    capture_query_outcome,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio

FAIL = "SELECT c FROM ShopDV.customers b UNNEST b.tags c;"
FIX = "SELECT c FROM ShopDV.customers b UNNEST split(b.tags, ',') c;"


# CaptureState


def test_failure_then_different_success_captures_a_note() -> None:
    state = CaptureState()
    assert state.record(FAIL, "QUERY_ERROR") == []
    captured = state.record(FIX, None)
    assert len(captured) == 1
    subject, note = captured[0]
    assert subject == "ShopDV.customers"
    assert "QUERY_ERROR" in note
    assert "working form" in note


def test_identical_statement_success_is_not_a_fix() -> None:
    # A retry that succeeds unchanged (transient failure) teaches nothing.
    state = CaptureState()
    state.record(FAIL, "NOT_READY")
    assert state.record(FAIL, None) == []


def test_success_without_pending_failure_captures_nothing() -> None:
    assert CaptureState().record(FIX, None) == []


def test_pending_failure_is_consumed_once() -> None:
    state = CaptureState()
    state.record(FAIL, "QUERY_ERROR")
    assert len(state.record(FIX, None)) == 1
    assert state.record(FIX, None) == []


def test_trim_flattens_and_truncates() -> None:
    long = "SELECT\n  a,\n  b " + "x" * MAX_CAPTURED_STATEMENT_LEN
    trimmed = _trim(long)
    assert "\n" not in trimmed
    assert trimmed.endswith("...")
    assert len(trimmed) == MAX_CAPTURED_STATEMENT_LEN + 3


# capture_query_outcome


async def test_capture_disabled_issues_no_writes(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    state = CaptureState()
    await capture_query_outcome(
        cap.client, settings, state, statement=FAIL, result_error="QUERY_ERROR"
    )
    await capture_query_outcome(cap.client, settings, state, statement=FIX, result_error=None)
    assert cap.requests == []


async def test_capture_persists_fix_note_through_memory_write(settings: Settings) -> None:
    settings = settings.model_copy(update={"memory_write_enabled": True})
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    state = CaptureState()

    await capture_query_outcome(
        cap.client, settings, state, statement=FAIL, result_error="QUERY_ERROR"
    )
    assert cap.requests == []  # a failure alone writes nothing
    await capture_query_outcome(cap.client, settings, state, statement=FIX, result_error=None)

    # memory_write path: current-row lookup, then the INSERT of the new note.
    statements = [parse_qs(r.content.decode())["statement"][0] for r in cap.requests]
    assert any("INSERT INTO AgentMemory.Memory" in s for s in statements)
    insert_form = parse_qs(cap.requests[-1].content.decode())
    row = json.loads(insert_form["$row"][0])
    assert row["subject"] == "ShopDV.customers"
    assert "working form" in row["text"]


async def test_session_log_appends_events(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"session_log_dir": str(tmp_path)})
    cap = make_capturing_cc(settings)
    state = CaptureState()

    await capture_query_outcome(
        cap.client, settings, state, statement=FAIL, result_error="QUERY_ERROR"
    )
    await capture_query_outcome(cap.client, settings, state, statement=FIX, result_error=None)

    log = tmp_path / f"{settings.agent_session_id}.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["outcome"] for e in events] == ["error", "success"]
    assert events[0]["error"] == "QUERY_ERROR"
    assert events[0]["statement"] == FAIL


async def test_unwritable_session_log_is_swallowed(settings: Settings, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not a directory")
    settings = settings.model_copy(update={"session_log_dir": str(blocker)})
    cap = make_capturing_cc(settings)

    await capture_query_outcome(
        cap.client, settings, CaptureState(), statement=FAIL, result_error=None
    )
    assert cap.requests == []


async def test_captured_fix_note_is_grounded_by_working_statement(settings: Settings) -> None:
    settings = settings.model_copy(update={"memory_write_enabled": True})
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    state = CaptureState()
    await capture_query_outcome(
        cap.client, settings, state, statement=FAIL, result_error="QUERY_ERROR"
    )
    await capture_query_outcome(cap.client, settings, state, statement=FIX, result_error=None)
    row = json.loads(parse_qs(cap.requests[-1].content.decode())["$row"][0])
    assert row["source_query"] == FIX
