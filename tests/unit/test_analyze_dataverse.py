"""Unit tests for the analyze_dataverse prompt (pure template + selection threshold)."""

from __future__ import annotations

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.prompts import STORAGE_FORMAT_AWARENESS_BLOCK
from asterixdb_mcp.prompts.analyze_dataverse import (
    DATASET_SELECTION_THRESHOLD,
    compose_analyze_dataverse,
    run_analyze_dataverse,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def test_compose_includes_inventory_and_storage_block() -> None:
    text = compose_analyze_dataverse("DV", ["a", "b"], total=2)
    assert "Exploring Dataverse `DV`" in text
    assert "- `a`" in text and "- `b`" in text
    assert STORAGE_FORMAT_AWARENESS_BLOCK in text
    assert "READ-ONLY" in text


def test_compose_requests_dataset_selection_when_over_threshold() -> None:
    text = compose_analyze_dataverse(
        "DV", ["x"], total=DATASET_SELECTION_THRESHOLD + 1, needs_dataset_selection=True
    )
    assert "Re-invoke this prompt with a specific `dataset`" in text


def test_compose_embeds_schema_when_provided() -> None:
    text = compose_analyze_dataverse("DV", ["x"], total=1, schema={"format": "ROW"})
    assert "Embedded Schema" in text
    assert '"format": "ROW"' in text


async def test_run_prompts_for_dataset_on_large_dataverse(settings: Settings) -> None:
    many = [{"DataverseName": "DV", "DatasetName": f"d{i}"} for i in range(15)]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": many})
    text = await run_analyze_dataverse(cap.client, settings, dataverse="DV")
    assert "specific `dataset`" in text


async def test_run_without_dataverse_returns_guidance_and_no_cc_call(
    settings: Settings,
) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    text = await run_analyze_dataverse(cap.client, settings)
    assert "No `dataverse` was provided" in text
    assert "list_dataverses" in text
    # A missing argument must not hit the cluster.
    assert cap.requests == []


async def test_run_returns_error_text_when_listing_fails(settings: Settings) -> None:
    body = {"status": "fatal", "errors": [{"code": "ASX0", "msg": "down"}]}
    cap = make_capturing_cc(settings, response_json=body)
    text = await run_analyze_dataverse(cap.client, settings, dataverse="DV")
    assert "Could not list datasets" in text


async def test_run_embeds_schema_when_dataset_given(settings: Settings) -> None:
    dataset = {
        "DataverseName": "DV",
        "DatasetName": "Events",
        "DatatypeName": "T",
        "DatasetFormat": {"Format": "row"},
        "InternalDetails": {"PrimaryKey": [["id"]]},
    }

    def handler(request: object) -> object:  # dispatch by metadata collection
        from urllib.parse import parse_qs

        import httpx

        stmt = parse_qs(request.content.decode())["statement"][0]
        rows = [dataset] if "`Dataset`" in stmt else []
        return httpx.Response(200, json={"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    text = await run_analyze_dataverse(cap.client, settings, dataverse="DV", dataset="Events")
    assert "Embedded Schema" in text
    assert '"format": "ROW"' in text


async def test_run_embeds_error_when_schema_fetch_fails(settings: Settings) -> None:
    from urllib.parse import parse_qs

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        # The list query has no "DatasetName =" filter; the get_schema lookup does.
        if "DatasetName =" in stmt:
            rows: list = []  # dataset not found -> get_schema errors
        elif "`Dataset`" in stmt:
            rows = [{"DataverseName": "DV", "DatasetName": "Ghost"}]
        else:
            rows = []
        return httpx.Response(200, json={"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    text = await run_analyze_dataverse(cap.client, settings, dataverse="DV", dataset="Ghost")
    assert "Embedded Schema" in text
    assert "error" in text
