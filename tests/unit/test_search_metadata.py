"""Unit tests for search_metadata."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.search_metadata import MAX_QUERY_LEN, run_search_metadata
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _table_of(req: httpx.Request) -> str:
    stmt = parse_qs(req.content.decode()).get("statement", [""])[0]
    return stmt


def _catalog_handler(rows_by_table: dict[str, list[dict]]):
    def handler(req: httpx.Request) -> httpx.Response:
        stmt = _table_of(req)
        for table, rows in rows_by_table.items():
            if f"Metadata.`{table}`" in stmt:
                return httpx.Response(200, json={"status": "success", "results": rows})
        return httpx.Response(200, json={"status": "success", "results": []})

    return handler


async def test_empty_query_rejected(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_search_metadata(cap.client, settings, query="  ")
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


async def test_overlong_query_rejected(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_search_metadata(cap.client, settings, query="x" * (MAX_QUERY_LEN + 1))
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value


async def test_ranks_exact_over_prefix_over_substring(settings: Settings) -> None:
    handler = _catalog_handler(
        {
            "Dataset": [
                {"DatasetName": "customer", "DataverseName": "S"},
                {"DatasetName": "customer_archive", "DataverseName": "S"},
                {"DatasetName": "old_customer_data", "DataverseName": "S"},
                {"DatasetName": "zzz", "DataverseName": "S"},
            ]
        }
    )
    cap = make_capturing_cc(settings, handler=handler)
    result = await run_search_metadata(cap.client, settings, query="customer")
    names = [m["name"] for m in result.structured["matches"]]
    assert names[0] == "customer"  # exact
    assert names[1] == "customer_archive"  # prefix
    assert "old_customer_data" in names  # substring
    assert "zzz" not in names  # no match


async def test_index_match_carries_dataset(settings: Settings) -> None:
    handler = _catalog_handler(
        {"Index": [{"IndexName": "byCity", "DataverseName": "S", "DatasetName": "Orders"}]}
    )
    cap = make_capturing_cc(settings, handler=handler)
    result = await run_search_metadata(cap.client, settings, query="byCity")
    match = result.structured["matches"][0]
    assert match["kind"] == "index"
    assert match["dataset"] == "Orders"


async def test_similarity_tail_matches_near_names(settings: Settings) -> None:
    handler = _catalog_handler({"Function": [{"Name": "calculate", "DataverseName": "S"}]})
    cap = make_capturing_cc(settings, handler=handler)
    # "calculat" is not a substring of "calculate"? it is. Use a fuzzy near-miss.
    result = await run_search_metadata(cap.client, settings, query="calcualte")
    assert result.structured["totalMatches"] >= 1


async def test_limit_caps_results(settings: Settings) -> None:
    rows = [{"DatasetName": f"item{n}", "DataverseName": "S"} for n in range(50)]
    cap = make_capturing_cc(settings, handler=_catalog_handler({"Dataset": rows}))
    result = await run_search_metadata(cap.client, settings, query="item", limit=5)
    assert len(result.structured["matches"]) == 5
    assert result.structured["totalMatches"] == 50


async def test_per_collection_failure_tolerated(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        stmt = _table_of(req)
        if "Metadata.`Dataset`" in stmt:
            return httpx.Response(200, json={"status": "success", "results": [
                {"DatasetName": "thing", "DataverseName": "S"}
            ]})
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_search_metadata(cap.client, settings, query="thing")
    # Dataset collection succeeded; others degraded to empty.
    assert result.structured["totalMatches"] == 1


async def test_skips_non_dict_and_unnamed_rows(settings: Settings) -> None:
    handler = _catalog_handler(
        {"Dataset": [{"DatasetName": "ok", "DataverseName": "S"}, "junk", {"x": 1}]}
    )
    cap = make_capturing_cc(settings, handler=handler)
    result = await run_search_metadata(cap.client, settings, query="ok")
    assert result.structured["totalMatches"] == 1
