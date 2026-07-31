"""Unit tests for the bounds on the memory subsystem's growth and scans.

Findings H1, H2, H3, C4, M3. Every one of them is the same shape: something that
is small in a single-user session and unbounded in a long-lived multi-client
gateway. None of them fails visibly until it fails completely.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.decay import CANDIDATE_SCAN_LIMIT
from asterixdb_mcp.distill import EVENT_SCAN_LIMIT, EVENTS_QUERY, fetch_cluster_events
from asterixdb_mcp.tools.memory_capture import MAX_CAPTURED_STATEMENT_LEN, _build_event
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _settings(**overrides: object) -> Settings:
    return Settings(
        cc_base_url="http://test-cc:19002",
        agent_session_id="sess-test",
        **overrides,
    )


def _statements(cap) -> list[str]:
    return [parse_qs(r.content.decode())["statement"][0] for r in cap.requests]


# H1 — the event scan must not pull the whole log into the process


def test_the_event_query_is_windowed_by_time() -> None:
    # Without a window this read the entire episodic log into a Python list on
    # every startup and every distill interval.
    assert "$since" in EVENTS_QUERY


def test_the_event_query_is_limited() -> None:
    # The window bounds how far back it reaches; the limit bounds what a burst
    # inside that window can do.
    assert f"LIMIT {EVENT_SCAN_LIMIT}" in EVENTS_QUERY


async def test_the_event_scan_binds_a_window_that_is_in_the_past() -> None:
    settings = _settings()
    cap = make_capturing_cc(settings, principal="tenant-a")

    await fetch_cluster_events(cap.client, "sess::t::1", settings)

    since = float(parse_qs(cap.requests[-1].content.decode())["$since"][0])
    assert 0 < since < time.time()


async def test_a_wider_retention_window_reaches_further_back() -> None:
    narrow = _settings(event_retention_days=1)
    wide = _settings(event_retention_days=30)
    cap_narrow = make_capturing_cc(narrow, principal="tenant-a")
    cap_wide = make_capturing_cc(wide, principal="tenant-a")

    await fetch_cluster_events(cap_narrow.client, "sess::t::1", narrow)
    await fetch_cluster_events(cap_wide.client, "sess::t::1", wide)

    since_narrow = float(parse_qs(cap_narrow.requests[-1].content.decode())["$since"][0])
    since_wide = float(parse_qs(cap_wide.requests[-1].content.decode())["$since"][0])
    assert since_wide < since_narrow


# H3 — event statements must not be stored whole


def test_a_stored_event_statement_is_trimmed() -> None:
    # _trim was applied to notes and never to events, so row size was unbounded.
    event = _build_event(_settings(), "SELECT " + "x" * 5000, None)

    assert len(event["statement"]) <= MAX_CAPTURED_STATEMENT_LEN + 3


def test_a_short_statement_is_stored_whole() -> None:
    event = _build_event(_settings(), "SELECT 1;", None)

    assert event["statement"] == "SELECT 1;"


# C4 — "distinct sessions" must mean distinct conversations, not restarts


def test_an_event_records_the_connection_it_came_from() -> None:
    # Distillation promotes a statement after success in >=2 distinct sessions.
    # Stamped with the process id, that counted gateway restarts.
    event = _build_event(_settings(), "SELECT 1;", None, session="conn-abc")

    assert event["session"] == "conn-abc"


def test_two_connections_of_one_process_are_distinct_sessions() -> None:
    settings = _settings()

    first = _build_event(settings, "SELECT 1;", None, session="conn-a")
    second = _build_event(settings, "SELECT 1;", None, session="conn-b")

    assert first["session"] != second["session"]


def test_an_event_with_no_connection_falls_back_to_the_process() -> None:
    # Maintenance and other callerless paths still have to record something, and
    # the process id is the honest answer there.
    event = _build_event(_settings(), "SELECT 1;", None)

    assert event["session"] == "sess-test"


# M3 — the decay scan must not grow with the store


async def test_the_decay_scan_is_limited() -> None:
    from asterixdb_mcp.decay import run_decay

    settings = _settings(memory_write_enabled=True)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "results": []})

    cap = make_capturing_cc(settings, handler=handler, principal="tenant-a")

    await run_decay(cap.client, settings)

    assert f"LIMIT {CANDIDATE_SCAN_LIMIT}" in _statements(cap)[0]
