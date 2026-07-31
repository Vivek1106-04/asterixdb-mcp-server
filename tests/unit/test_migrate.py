"""Unit tests for the ownership backfill.

Rows written before memory was tenant-scoped carry no owner, and a scoped read
matches neither the caller's principal nor the global tier — so without this
pass they would simply stop appearing.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.memory_store import PRINCIPAL_FIELD
from asterixdb_mcp.migrate import backfill_principals
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


@pytest.fixture
def write_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"memory_write_enabled": True})


def _handler(memory: list[dict], events: list[dict]):
    def handle(request: httpx.Request) -> httpx.Response:
        statement = parse_qs(request.content.decode())["statement"][0]
        if statement.lstrip().startswith("SELECT"):
            rows = events if "SessionEvent" in statement else memory
            return httpx.Response(200, json={"status": "success", "results": rows})
        return httpx.Response(200, json={"status": "success", "results": []})

    return handle


def _written(cap) -> list[dict]:
    return [
        json.loads(form["$row"][0])
        for form in (parse_qs(request.content.decode()) for request in cap.requests)
        if "$row" in form
    ]


async def test_an_unowned_row_is_adopted_by_the_backfilling_principal(
    write_settings: Settings,
) -> None:
    cap = make_capturing_cc(
        write_settings, handler=_handler([{"id": "n1"}], []), principal="tenant-a"
    )

    await backfill_principals(cap.client, write_settings)

    assert [row[PRINCIPAL_FIELD] for row in _written(cap)] == ["tenant-a"]


async def test_session_events_are_backfilled_too(write_settings: Settings) -> None:
    cap = make_capturing_cc(
        write_settings, handler=_handler([], [{"id": "e1"}]), principal="tenant-a"
    )

    await backfill_principals(cap.client, write_settings)

    assert [row["id"] for row in _written(cap)] == ["e1"]


async def test_the_backfill_reports_what_it_adopted(write_settings: Settings) -> None:
    cap = make_capturing_cc(
        write_settings, handler=_handler([{"id": "n1"}], [{"id": "e1"}]), principal="tenant-a"
    )

    counts = await backfill_principals(cap.client, write_settings)

    assert counts == {"concepts": 1, "events": 1}


async def test_a_store_with_no_unowned_rows_writes_nothing(write_settings: Settings) -> None:
    cap = make_capturing_cc(write_settings, handler=_handler([], []), principal="tenant-a")

    counts = await backfill_principals(cap.client, write_settings)

    assert counts == {"concepts": 0, "events": 0}
    assert _written(cap) == []


async def test_the_backfill_only_looks_at_rows_that_have_no_owner(
    write_settings: Settings,
) -> None:
    # Adopting a row that already names a tenant would move it between tenants.
    cap = make_capturing_cc(write_settings, handler=_handler([], []), principal="tenant-a")

    await backfill_principals(cap.client, write_settings)

    statements = [parse_qs(request.content.decode())["statement"][0] for request in cap.requests]
    assert statements and all(f"{PRINCIPAL_FIELD} IS UNKNOWN" in stmt for stmt in statements)


async def test_a_read_only_gateway_skips_the_backfill(settings: Settings) -> None:
    # Nothing can be written with memory writes off, and raising would only turn
    # a startup pass into a logged failure on every boot.
    cap = make_capturing_cc(settings, handler=_handler([{"id": "n1"}], []), principal="tenant-a")

    counts = await backfill_principals(cap.client, settings)

    assert counts == {"concepts": 0, "events": 0}
    assert cap.requests == []
