"""Unit tests for the gateway-side OKF walk (asterixdb_mcp.okf_walk).

The pure reconcile core is exercised in depth by tests/unit/test_okf_scripts.py
through the script re-exports; here we cover the async walk path: fetching the
bundle, bootstrap DDL, and parameter-bound bi-temporal writes.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.okf_walk import (
    BOOTSTRAP_STATEMENTS,
    bootstrap_store,
    fetch_bundle,
    fetch_current,
    run_walk,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


@pytest.fixture
def write_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"memory_write_enabled": True})


_CATALOG_DOC = {
    "subject": "ShopDV.orders",
    "type": "AsterixDB Dataset",
    "text": "core v2",
    "links": ["ShopDV"],
}
_SELF_DOC = {"subject": "AgentMemory.Memory", "type": "AsterixDB Dataset", "text": "self"}
_CURRENT_ROW = {
    "id": "ShopDV.orders@t0",
    "subject": "ShopDV.orders",
    "type": "AsterixDB Dataset",
    "kind": "semantic",
    "core": "core v1",
    "overlay": "learned note\n",
    "text": "core v1\n\nlearned note\n",
    "valid_from": "t0",
}


def _walk_handler(catalog_rows: list, current_rows: list):
    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        if "okf_catalog" in stmt:
            rows = catalog_rows
        elif "valid_to IS UNKNOWN" in stmt:
            rows = current_rows
        else:
            rows = []
        return httpx.Response(200, json={"status": "success", "results": rows})

    return handler


async def test_fetch_bundle_excludes_the_store_itself(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_walk_handler([_CATALOG_DOC, _SELF_DOC, "junk"], []))
    bundle = await fetch_bundle(cap.client, "ccid")
    assert list(bundle) == ["ShopDV.orders"]


async def test_fetch_current_keys_rows_by_subject(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_walk_handler([], [_CURRENT_ROW, "junk"]))
    current = await fetch_current(cap.client, "ccid")
    assert list(current) == ["ShopDV.orders"]


async def test_bootstrap_store_runs_exact_ddl(write_settings: Settings) -> None:
    cap = make_capturing_cc(write_settings)
    await bootstrap_store(cap.client, "ccid")
    statements = [parse_qs(r.content.decode())["statement"][0] for r in cap.requests]
    assert statements == list(BOOTSTRAP_STATEMENTS)


async def test_run_walk_supersedes_and_reinserts_changed_concept(
    write_settings: Settings,
) -> None:
    cap = make_capturing_cc(write_settings, handler=_walk_handler([_CATALOG_DOC], [_CURRENT_ROW]))
    summary = await run_walk(cap.client, write_settings)
    assert summary == {"concepts": 1, "inserted": 1, "superseded": 1, "unchanged": 0}
    writes = [
        parse_qs(r.content.decode())
        for r in cap.requests
        if "AgentMemory.Memory ([$row])" in parse_qs(r.content.decode())["statement"][0]
    ]
    superseded = json.loads(writes[0]["$row"][0])
    inserted = json.loads(writes[1]["$row"][0])
    assert superseded["valid_to"]  # old row retired, never deleted
    assert inserted["core"] == "core v2"
    assert inserted["overlay"] == "learned note\n"  # overlay survives the re-walk


async def test_run_walk_unchanged_catalog_writes_nothing(write_settings: Settings) -> None:
    unchanged_doc = {**_CATALOG_DOC, "text": "core v1"}
    cap = make_capturing_cc(write_settings, handler=_walk_handler([unchanged_doc], [_CURRENT_ROW]))
    summary = await run_walk(cap.client, write_settings)
    assert summary["unchanged"] == 1 and summary["inserted"] == 0
    statements = [parse_qs(r.content.decode())["statement"][0] for r in cap.requests]
    assert not any("INSERT" in s or "UPSERT" in s for s in statements)


async def test_run_walk_inserts_new_concept_with_no_prior_row(
    write_settings: Settings,
) -> None:
    cap = make_capturing_cc(write_settings, handler=_walk_handler([_CATALOG_DOC], []))
    summary = await run_walk(cap.client, write_settings)
    assert summary == {"concepts": 1, "inserted": 1, "superseded": 0, "unchanged": 0}
    form = parse_qs(cap.requests[-1].content.decode())
    row = json.loads(form["$row"][0])
    assert row["core"] == "core v2" and "overlay" not in row
