"""Unit tests for the offline event buffer's size cap.

The buffer is what a query outcome falls back to when the cluster will not take
it. Uncapped, an agent that can provoke query outcomes can fill the disk, and a
full disk takes the gateway down for every tenant — so the cap is a tenancy
control, not a housekeeping detail.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.tools import memory_capture
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _settings(tmp_path: Path, cap: int) -> Settings:
    return Settings(
        cc_base_url="http://test-cc:19002",
        agent_session_id="sess-test",
        session_log_dir=str(tmp_path),
        session_log_max_bytes=cap,
    )


def _event(n: int) -> dict[str, object]:
    return {"id": f"e{n}", "statement": "x" * 100}


def _buffer(tmp_path: Path) -> Path:
    return tmp_path / "sess-test.jsonl"


@pytest.fixture(autouse=True)
def _fresh_report_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # Whether a full buffer has been reported is process state by nature; each
    # test needs to start from a process that has not yet seen one.
    monkeypatch.setattr(memory_capture, "_buffer_full_reported", False)


def test_an_event_is_buffered_while_there_is_room(tmp_path: Path) -> None:
    settings = _settings(tmp_path, cap=10_000)

    memory_capture._append_jsonl_event(settings, _event(1))

    assert len(_buffer(tmp_path).read_text().splitlines()) == 1


def test_events_are_dropped_once_the_buffer_is_full(tmp_path: Path) -> None:
    # The budget is a threshold checked before writing, so the buffer can
    # overshoot by at most the one event that crossed it — bounded, which is the
    # property that matters, rather than exact.
    settings = _settings(tmp_path, cap=400)

    for n in range(200):
        memory_capture._append_jsonl_event(settings, _event(n))

    assert _buffer(tmp_path).stat().st_size < 400 + 300


def test_the_cap_covers_the_whole_buffer_directory(tmp_path: Path) -> None:
    # One file per session id, and a crashed session's file is never reclaimed
    # by its successor. Capping each file alone would let N restarts cost N caps.
    (tmp_path / "old-session.jsonl").write_text("x" * 500)
    settings = _settings(tmp_path, cap=400)

    memory_capture._append_jsonl_event(settings, _event(1))

    assert not _buffer(tmp_path).exists()


def test_a_full_buffer_is_reported_once_not_per_event(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # An agent provoking a flood of outcomes must not be able to turn the drop
    # path into its own log flood.
    settings = _settings(tmp_path, cap=200)
    memory_capture._append_jsonl_event(settings, _event(0))

    with caplog.at_level(logging.WARNING):
        for n in range(1, 10):
            memory_capture._append_jsonl_event(settings, _event(n))

    assert len([r for r in caplog.records if "buffer" in r.message]) == 1


def test_buffering_resumes_when_room_is_freed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path, cap=200)
    for n in range(4):
        memory_capture._append_jsonl_event(settings, _event(n))  # fills, then warns

    _buffer(tmp_path).unlink()
    caplog.clear()  # the first spell's warning is not what this test is about
    with caplog.at_level(logging.WARNING):
        for n in range(4, 8):
            memory_capture._append_jsonl_event(settings, _event(n))

    assert _buffer(tmp_path).exists()  # buffering resumed
    assert len([r for r in caplog.records if "buffer" in r.message]) == 1  # and warned afresh


def test_an_unreadable_buffer_directory_does_not_raise(tmp_path: Path) -> None:
    # Measuring the budget is best-effort; a buffer that cannot be sized must not
    # take down the query it was recording.
    settings = _settings(tmp_path / "missing", cap=400)

    memory_capture._append_jsonl_event(settings, _event(1))


def test_the_default_cap_is_bounded(tmp_path: Path) -> None:
    assert 0 < Settings().session_log_max_bytes <= 64 * 1024 * 1024


# M1/M2 — the buffer must not be touched synchronously on a user's query


async def test_recording_an_event_does_not_replay_the_backlog(tmp_path: Path) -> None:
    # M2: replay was awaited inside capture_query_outcome, so the first query
    # after an outage absorbed the entire backlog, one INSERT per line.
    settings = _settings(tmp_path, cap=10_000).model_copy(update={"memory_write_enabled": True})
    (tmp_path / "old-session.jsonl").write_text('{"id": "e1"}\n{"id": "e2"}\n')
    cap = make_capturing_cc(settings, principal="tenant-a")

    await memory_capture._record_session_event(cap.client, settings, "SELECT 1;", None)

    # One insert for this event, and nothing for the two buffered lines.
    assert len(cap.requests) == 1


async def test_maintenance_is_what_drains_the_backlog(tmp_path: Path) -> None:
    settings = _settings(tmp_path, cap=10_000).model_copy(update={"memory_write_enabled": True})
    (tmp_path / "old-session.jsonl").write_text('{"id": "e1"}\n{"id": "e2"}\n')
    cap = make_capturing_cc(settings, principal="tenant-a")

    await memory_capture.flush_buffered_events(cap.client, settings)

    assert len(cap.requests) == 2
    assert not (tmp_path / "old-session.jsonl").exists()


async def test_buffer_writes_do_not_block_the_event_loop(tmp_path: Path) -> None:
    # M1: read_text/open/unlink ran inside awaited coroutines on the request
    # path. Individually microseconds; under load with a large buffer, a stall.
    import inspect

    source = inspect.getsource(memory_capture._record_session_event)

    assert "to_thread" in source


async def test_a_gateway_with_no_buffer_directory_has_nothing_to_drain() -> None:
    settings = Settings(cc_base_url="http://test-cc:19002", agent_session_id="sess-test")
    cap = make_capturing_cc(settings, principal="tenant-a")

    await memory_capture.flush_buffered_events(cap.client, settings)

    assert cap.requests == []
