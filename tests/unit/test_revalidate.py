"""Unit tests for proof replay.

Detection already worked: the gateway could see a stored claim and fresh evidence
disagree, and it said so. What it never did was write anything down. Three
sessions against deliberately mutated data produced zero corrective writes, even
with the disagreement named in the response — a model told a note is wrong will
use the fresh number and move on, leaving the wrong note for the next session.

So the write has to be the gateway's, and this is the one case where it can be
made without inference: the note nominated the query that proves it, and that
query now disproves it.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.revalidate import REVALIDATE_LIMIT, is_replayable, run_revalidation
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _settings() -> Settings:
    return Settings(
        cc_base_url="http://test-cc:19002",
        agent_session_id="sess-test",
        memory_write_enabled=True,
    )


def _note(**overrides: object) -> dict:
    row = {
        "id": "ShopDV.orders@1",
        "subject": "ShopDV.orders",
        "type": "Note",
        "kind": "semantic",
        "text": "Orders with status Operational have a count of 410.",
        "source_query": "SELECT status, COUNT(*) AS count FROM ShopDV.orders GROUP BY status;",
        "valid_from": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _handler(notes: list[dict], proof: object):
    """Serve the note scan, then whatever the replayed proof should return."""

    def handle(request: httpx.Request) -> httpx.Response:
        statement = parse_qs(request.content.decode())["statement"][0]
        if statement.lstrip().startswith(("INSERT", "UPSERT")):
            return httpx.Response(200, json={"status": "success", "results": []})
        if "AgentMemory" in statement:
            return httpx.Response(200, json={"status": "success", "results": notes})
        if isinstance(proof, Exception):
            return httpx.Response(500, json={"status": "fatal", "errors": [{"msg": "gone"}]})
        return httpx.Response(200, json={"status": "success", "results": proof})

    return handle


def _writes(cap) -> list[dict]:
    return [
        json.loads(form["$row"][0])
        for form in (parse_qs(r.content.decode()) for r in cap.requests)
        if "$row" in form
    ]


# what is replayable at all


def test_a_grounded_note_is_replayable() -> None:
    assert is_replayable(_note()) is True


def test_a_note_with_no_proof_is_not_replayable() -> None:
    # Nothing to re-run, so nothing can be disproved. Ungrounded notes stay in
    # the flag-only regime the staleness detector already provides.
    assert is_replayable(_note(source_query=None)) is False


def test_a_superseded_note_is_not_replayable() -> None:
    assert is_replayable(_note(valid_to="2026-02-01T00:00:00+00:00")) is False


def test_a_preference_is_not_replayable() -> None:
    # A style rule has no proving query and no fact to contradict.
    assert is_replayable(_note(type="Preference", kind="preference")) is False


# the replay itself


async def test_a_note_its_own_proof_disproves_is_retired() -> None:
    settings = _settings()
    cap = make_capturing_cc(
        settings,
        handler=_handler([_note()], [{"status": "Operational", "count": 290}]),
        principal="tenant-a",
    )

    counts = await run_revalidation(cap.client, settings)

    retired = [row for row in _writes(cap) if row.get("valid_to")]
    assert counts["retired"] == 1
    assert len(retired) == 1
    assert "410" in retired[0]["archived_reason"] and "290" in retired[0]["archived_reason"]


async def test_a_note_its_proof_still_supports_is_left_alone() -> None:
    settings = _settings()
    cap = make_capturing_cc(
        settings,
        handler=_handler([_note()], [{"status": "Operational", "count": 410}]),
        principal="tenant-a",
    )

    counts = await run_revalidation(cap.client, settings)

    assert counts["retired"] == 0
    assert [row for row in _writes(cap) if row.get("valid_to")] == []


async def test_a_retired_note_keeps_its_history() -> None:
    # Bi-temporal, like every other supersede: the disproved claim stays
    # readable as history rather than being deleted.
    settings = _settings()
    cap = make_capturing_cc(
        settings,
        handler=_handler([_note()], [{"status": "Operational", "count": 290}]),
        principal="tenant-a",
    )

    await run_revalidation(cap.client, settings)

    retired = next(row for row in _writes(cap) if row.get("valid_to"))
    assert retired["id"] == "ShopDV.orders@1"
    assert retired["text"] == _note()["text"]


async def test_a_proof_that_will_not_run_is_flagged_not_retired() -> None:
    # The distinction that matters: "the proof returned contradicting evidence"
    # is disproof; "the proof could not run" is an unknown. Retiring on an
    # unknown would delete good knowledge whenever a cluster hiccups.
    settings = _settings()
    cap = make_capturing_cc(
        settings, handler=_handler([_note()], RuntimeError()), principal="tenant-a"
    )

    counts = await run_revalidation(cap.client, settings)

    written = _writes(cap)
    assert counts["retired"] == 0
    assert counts["unprovable"] == 1
    assert written and written[0].get("suspect_since")
    assert not written[0].get("valid_to")


async def test_a_proof_returning_nothing_is_not_disproof() -> None:
    # An empty result says the query matched no rows, which is not the same as
    # the claim being false, and is exactly what a mid-migration dataset returns.
    settings = _settings()
    cap = make_capturing_cc(settings, handler=_handler([_note()], []), principal="tenant-a")

    counts = await run_revalidation(cap.client, settings)

    assert counts["retired"] == 0


async def test_the_pass_is_bounded() -> None:
    settings = _settings()
    cap = make_capturing_cc(settings, handler=_handler([], []), principal="tenant-a")

    await run_revalidation(cap.client, settings)

    scan = parse_qs(cap.requests[0].content.decode())["statement"][0]
    assert f"LIMIT {REVALIDATE_LIMIT}" in scan


async def test_a_read_only_gateway_replays_nothing() -> None:
    # Replay exists to write the correction. With writes off it could only
    # re-run queries and discard the answer, which is pure cost.
    settings = _settings().model_copy(update={"memory_write_enabled": False})
    cap = make_capturing_cc(
        settings, handler=_handler([_note()], [{"status": "Operational", "count": 290}])
    )

    counts = await run_revalidation(cap.client, settings)

    assert counts == {"checked": 0, "retired": 0, "unprovable": 0}
    assert cap.requests == []


async def test_a_row_with_no_proof_in_the_scan_is_skipped() -> None:
    # The scan filters on source_query, but the store is open-typed and a row
    # without one must not reach the replay.
    settings = _settings()
    cap = make_capturing_cc(
        settings,
        handler=_handler(["not-a-row", _note(source_query=None)], []),
        principal="tenant-a",
    )

    counts = await run_revalidation(cap.client, settings)

    assert counts["checked"] == 0


async def test_a_failed_correction_does_not_fail_the_pass() -> None:
    # Best-effort like every other maintenance write: a store that will not take
    # the retirement leaves the note standing rather than stopping the pass.
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        statement = parse_qs(request.content.decode())["statement"][0]
        if statement.lstrip().startswith(("INSERT", "UPSERT")):
            return httpx.Response(500, json={"status": "fatal", "errors": [{"msg": "no"}]})
        if "AgentMemory" in statement:
            return httpx.Response(200, json={"status": "success", "results": [_note()]})
        return httpx.Response(
            200, json={"status": "success", "results": [{"status": "Operational", "count": 290}]}
        )

    cap = make_capturing_cc(settings, handler=handler, principal="tenant-a")

    counts = await run_revalidation(cap.client, settings)

    assert counts["retired"] == 1


async def test_an_unreadable_store_ends_the_pass_quietly() -> None:
    settings = _settings()
    cap = make_capturing_cc(
        settings,
        response_json={"status": "fatal", "errors": [{"msg": "CC down"}]},
        status_code=500,
        principal="tenant-a",
    )

    assert await run_revalidation(cap.client, settings) == {
        "checked": 0,
        "retired": 0,
        "unprovable": 0,
    }
