"""Unit tests for deterministic error->fix capture and session logging."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType, GatewayError
from asterixdb_mcp.tools import ToolResult
from asterixdb_mcp.tools.memory_capture import (
    MAX_CAPTURED_STATEMENT_LEN,
    CaptureState,
    _trim,
    capture_error_signal,
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
    # a failure alone writes no NOTE; only its episodic event is recorded
    statements = [parse_qs(r.content.decode())["statement"][0] for r in cap.requests]
    assert all(s.startswith("INSERT INTO AgentMemory.SessionEvent") for s in statements)
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


# capture_error_signal


def test_error_signal_from_classified_error() -> None:
    result = ToolResult.error(GatewayError(ErrorType.SYNTAX_ERROR, "boom"))
    assert capture_error_signal(result) == ErrorType.SYNTAX_ERROR.value


def test_error_signal_error_without_type_is_none() -> None:
    assert capture_error_signal(ToolResult(text="x", structured={}, is_error=True)) is None


def test_error_signal_zero_rows_with_warnings_is_semantic() -> None:
    result = ToolResult(
        text="ok",
        structured={
            "status": "success",
            "rowsReturned": 0,
            "warnings": [{"code": 1, "msg": "ASX0002: Type mismatch: scan-collection"}],
        },
    )
    expected = "SEMANTIC_WARNING: ASX0002: Type mismatch: scan-collection"
    assert capture_error_signal(result) == expected


def test_error_signal_handles_string_warnings() -> None:
    result = ToolResult(
        text="ok", structured={"rowsReturned": 0, "warnings": ["plain warning"]}
    )
    assert capture_error_signal(result) == "SEMANTIC_WARNING: plain warning"


def test_error_signal_none_for_true_success_or_plain_empty() -> None:
    assert capture_error_signal(ToolResult(text="ok", structured={"rowsReturned": 5})) is None
    assert capture_error_signal(ToolResult(text="ok", structured={"rowsReturned": 0})) is None


# session events: cluster record, offline buffer, flush


async def test_event_recorded_on_cluster_when_writes_enabled(
    settings: Settings, tmp_path: Path
) -> None:
    settings = settings.model_copy(
        update={"memory_write_enabled": True, "session_log_dir": str(tmp_path)}
    )
    cap = make_capturing_cc(settings)
    await capture_query_outcome(
        cap.client, settings, CaptureState(), statement=FIX, result_error=None
    )
    form = parse_qs(cap.requests[0].content.decode())
    assert form["statement"][0].startswith("INSERT INTO AgentMemory.SessionEvent")
    event = json.loads(form["$row"][0])
    assert event["outcome"] == "success" and event["session"] == "sess-test" and event["id"]
    # cluster reachable -> nothing buffered on disk
    assert not (tmp_path / f"{settings.agent_session_id}.jsonl").exists()


async def test_event_falls_back_to_jsonl_when_cluster_unreachable(
    settings: Settings, tmp_path: Path
) -> None:
    settings = settings.model_copy(
        update={"memory_write_enabled": True, "session_log_dir": str(tmp_path)}
    )
    cap = make_capturing_cc(
        settings,
        response_json={"status": "fatal", "errors": [{"code": 1, "msg": "CC down"}]},
        status_code=500,
    )
    await capture_query_outcome(
        cap.client, settings, CaptureState(), statement=FAIL, result_error="QUERY_ERROR"
    )
    log = tmp_path / f"{settings.agent_session_id}.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["outcome"] for e in events] == ["error"]
    assert events[0]["error"] == "QUERY_ERROR"


async def test_buffered_events_flush_on_next_successful_write(
    settings: Settings, tmp_path: Path
) -> None:
    settings = settings.model_copy(
        update={"memory_write_enabled": True, "session_log_dir": str(tmp_path)}
    )
    log = tmp_path / f"{settings.agent_session_id}.jsonl"
    log.write_text(
        json.dumps({"session": "sess-test", "ts": 1.0, "statement": FIX, "outcome": "success"})
        + "\nnot json\n[1,2]\n"
    )
    cap = make_capturing_cc(settings)
    await capture_query_outcome(
        cap.client, settings, CaptureState(), statement=FIX, result_error=None
    )
    # live event insert + one replayed buffered event; buffer removed
    forms = [parse_qs(r.content.decode()) for r in cap.requests]
    assert all(f["statement"][0].startswith("INSERT INTO AgentMemory.SessionEvent") for f in forms)
    assert len(forms) == 2
    replayed = json.loads(forms[1]["$row"][0])
    assert replayed["ts"] == 1.0 and replayed["id"]  # id backfilled on replay
    assert not log.exists()


async def test_flush_survives_unreadable_buffer(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(
        update={"memory_write_enabled": True, "session_log_dir": str(tmp_path)}
    )
    # the buffer path exists but is a directory: read_text raises OSError
    (tmp_path / f"{settings.agent_session_id}.jsonl").mkdir()
    cap = make_capturing_cc(settings)
    await capture_query_outcome(
        cap.client, settings, CaptureState(), statement=FIX, result_error=None
    )
    assert len(cap.requests) == 1  # live event recorded; flush degraded silently


async def test_flush_survives_undeletable_buffer(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings.model_copy(
        update={"memory_write_enabled": True, "session_log_dir": str(tmp_path)}
    )
    log = tmp_path / f"{settings.agent_session_id}.jsonl"
    event = {"session": "s", "ts": 2.0, "statement": FIX, "outcome": "success"}
    log.write_text(json.dumps(event) + "\n")
    monkeypatch.setattr(Path, "unlink", lambda self: (_ for _ in ()).throw(OSError("busy")))
    cap = make_capturing_cc(settings)
    await capture_query_outcome(
        cap.client, settings, CaptureState(), statement=FIX, result_error=None
    )
    assert len(cap.requests) == 2  # live + replayed; failed unlink swallowed

# event enrichment (client identity + performance metrics)


def test_elapsed_ms_parses_cc_duration_strings() -> None:
    from asterixdb_mcp.tools.memory_capture import _elapsed_ms

    assert _elapsed_ms({"elapsedTime": "377.644875ms"}) == 377.645
    assert _elapsed_ms({"elapsedTime": "2.233s"}) == 2233.0
    assert _elapsed_ms({"elapsedTime": "150ns"}) == 0.0
    assert _elapsed_ms({"elapsedTime": "not-a-duration"}) is None
    assert _elapsed_ms({}) is None
    assert _elapsed_ms(None) is None


async def test_events_carry_client_and_metrics(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"session_log_dir": str(tmp_path)})
    cap = make_capturing_cc(settings)

    await capture_query_outcome(
        cap.client,
        settings,
        CaptureState(),
        statement=FIX,
        result_error=None,
        client_name="claude-desktop/1.2",
        metrics={"elapsedTime": "1.5s", "processedObjects": 908915},
    )

    log = tmp_path / f"{settings.agent_session_id}.jsonl"
    event = json.loads(log.read_text().splitlines()[0])
    assert event["client"] == "claude-desktop/1.2"
    assert event["elapsed_ms"] == 1500.0
    assert event["processed_objects"] == 908915


async def test_flush_replays_buffers_from_other_sessions(
    settings: Settings, tmp_path: Path
) -> None:
    # A crashed process leaves a buffer under ITS unique session id; the next
    # healthy session must still flush it to the cluster.
    settings = settings.model_copy(
        update={"memory_write_enabled": True, "session_log_dir": str(tmp_path)}
    )
    stale = {
        "id": "old-session@1",
        "session": "old-session",
        "statement": "SELECT 1;",
        "outcome": "success",
    }
    (tmp_path / "old-session.jsonl").write_text(json.dumps(stale) + "\n")
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})

    await capture_query_outcome(
        cap.client, settings, CaptureState(), statement=FIX, result_error=None
    )

    rows = [
        json.loads(parse_qs(r.content.decode())["$row"][0])
        for r in cap.requests
        if "SessionEvent" in parse_qs(r.content.decode())["statement"][0]
    ]
    assert any(row["id"] == "old-session@1" for row in rows)
    assert not (tmp_path / "old-session.jsonl").exists()


async def test_flush_degrades_when_log_dir_unlistable(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings.model_copy(
        update={"memory_write_enabled": True, "session_log_dir": str(tmp_path)}
    )
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})

    def unlistable(self: Path, pattern: str) -> list[Path]:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "glob", unlistable)
    # Must not raise: the current event still lands, the flush quietly skips.
    await capture_query_outcome(
        cap.client, settings, CaptureState(), statement=FIX, result_error=None
    )
    statements = [parse_qs(r.content.decode())["statement"][0] for r in cap.requests]
    assert any("SessionEvent" in s for s in statements)
