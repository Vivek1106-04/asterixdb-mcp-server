"""Unit tests for the describe_dataverse tool (batched, three-query form)."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.tools.describe_dataverse import MAX_DESCRIBE, run_describe_dataverse
from tests.conftest import json_response, make_capturing_cc

pytestmark = pytest.mark.anyio


def _handler(dataset_names: list[str]) -> object:
    """Serve the three batched queries: datasets, datatypes, indexes."""
    datasets = [
        {
            "DataverseName": "Yelp",
            "DatasetName": name,
            "DatatypeName": "T",
            "DatatypeDataverseName": "Yelp",
            "DatasetFormat": {"Format": "row"},
            "InternalDetails": {"PrimaryKey": [["id"]]},
        }
        for name in dataset_names
    ]
    datatypes = [
        {
            "DataverseName": "Yelp",
            "DatatypeName": "T",
            "Derived": {"Record": {"Fields": [{"FieldName": "id", "FieldType": "string"}]}},
        }
    ]
    indexes = (
        [{"DatasetName": dataset_names[0], "IndexName": "by_id", "IndexStructure": "BTREE"}]
        if dataset_names
        else []
    )

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        if "`Datatype`" in stmt:
            rows: list = datatypes
        elif "`Index`" in stmt:
            rows = indexes
        else:
            rows = datasets
        return json_response({"status": "success", "results": rows})

    return handler


async def test_describes_every_dataset_in_three_queries(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler(["A", "B"]))
    result = await run_describe_dataverse(cap.client, settings, dataverse="Yelp")
    assert result.is_error is False
    assert result.structured["datasetCount"] == 2
    assert result.structured["describedCount"] == 2
    assert result.structured["truncated"] is False
    assert {d["dataset"] for d in result.structured["datasets"]} == {"A", "B"}
    # Exactly three CC queries: datasets, datatypes, indexes.
    query_posts = [r for r in cap.requests if r.url.path == "/query/service"]
    assert len(query_posts) == 3


async def test_joins_fields_indexes_and_primary_key(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler(["A"]))
    result = await run_describe_dataverse(cap.client, settings, dataverse="Yelp")
    doc = result.structured["datasets"][0]
    assert doc["primaryKey"] == ["id"]
    assert doc["fields"] == [{"name": "id", "type": "string", "nullable": False}]
    assert doc["secondaryIndexes"][0]["indexName"] == "by_id"
    assert doc["datasetFormatInfo"]["format"] == "ROW"


async def test_case_insensitive_dataverse_resolution(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler(["A"]))
    result = await run_describe_dataverse(cap.client, settings, dataverse="yelp")
    assert result.structured["dataverse"] == "Yelp"


async def test_truncates_beyond_cap(settings: Settings) -> None:
    names = [f"ds{i}" for i in range(MAX_DESCRIBE + 3)]
    cap = make_capturing_cc(settings, handler=_handler(names))
    result = await run_describe_dataverse(cap.client, settings, dataverse="Yelp")
    assert result.structured["datasetCount"] == MAX_DESCRIBE + 3
    assert result.structured["describedCount"] == MAX_DESCRIBE
    assert result.structured["truncated"] is True
    assert "(truncated)" in result.text


async def test_unknown_dataverse_suggests_close_name(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler(["A"]))
    result = await run_describe_dataverse(cap.client, settings, dataverse="Ylp")
    assert result.is_error is True
    assert "Did you mean: Yelp?" in result.structured["errorMessage"]


async def test_unknown_dataverse_no_suggestion(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler(["A"]))
    result = await run_describe_dataverse(cap.client, settings, dataverse="zzzzzz")
    assert result.is_error is True
    assert result.structured["errorMessage"].endswith("was not found.")


async def test_cc_error_becomes_error_result(settings: Settings) -> None:
    body = {"status": "fatal", "errors": [{"code": "ASX9999", "msg": "boom"}]}
    cap = make_capturing_cc(settings, response_json=body)
    result = await run_describe_dataverse(cap.client, settings, dataverse="Yelp")
    assert result.is_error is True
    assert result.structured["errorType"] == "INTERNAL"


async def test_skips_malformed_dataset_and_index_names(settings: Settings) -> None:
    # Index with a non-string owner is skipped; a dataset whose name is not a
    # string still assembles, with no secondary indexes attached.
    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        if "`Datatype`" in stmt:
            rows: list = []
        elif "`Index`" in stmt:
            rows = [{"DatasetName": 123, "IndexName": "bad"}]
        else:
            rows = [{"DataverseName": "Yelp", "DatasetName": 999}]
        return json_response({"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_describe_dataverse(cap.client, settings, dataverse="Yelp")
    assert result.is_error is False
    assert result.structured["datasets"][0]["secondaryIndexes"] == []
