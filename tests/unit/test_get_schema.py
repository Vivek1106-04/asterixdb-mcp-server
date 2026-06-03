"""Unit tests for get_schema: pure metadata transforms + assembled document."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.get_schema import (
    extract_dataset_format_info,
    extract_primary_key,
    extract_record_fields,
    run_get_schema,
    summarize_secondary_indexes,
)
from tests.conftest import json_response, make_capturing_cc

pytestmark = pytest.mark.anyio


# pure transforms


def test_columnar_format_includes_projection_hint() -> None:
    info = extract_dataset_format_info({"DatasetFormat": {"Format": "column"}})
    assert info["format"] == "COLUMNAR"
    assert "projectionHint" in info


def test_row_is_default_when_block_absent_or_row() -> None:
    assert extract_dataset_format_info({})["format"] == "ROW"
    assert extract_dataset_format_info({"DatasetFormat": {"Format": "row"}})["format"] == "ROW"


def test_primary_key_joins_path_segments() -> None:
    record = {"InternalDetails": {"PrimaryKey": [["id"], ["addr", "zip"]]}}
    assert extract_primary_key(record) == ["id", "addr.zip"]


def test_primary_key_empty_when_no_internal_details() -> None:
    assert extract_primary_key({}) == []


def test_extract_record_fields() -> None:
    datatype = {
        "Derived": {
            "Record": {
                "Fields": [
                    {"FieldName": "id", "FieldType": "int64", "IsNullable": False},
                    {"FieldName": "name", "FieldType": "string", "IsNullable": True},
                ]
            }
        }
    }
    fields = extract_record_fields(datatype)
    assert fields == [
        {"name": "id", "type": "int64", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
    ]


def test_primary_key_handles_non_list_and_scalar_entries() -> None:
    # PrimaryKey not a list -> empty; scalar entries -> stringified.
    assert extract_primary_key({"InternalDetails": {"PrimaryKey": "nope"}}) == []
    assert extract_primary_key({"InternalDetails": {"PrimaryKey": ["id", ["a", "b"]]}}) == [
        "id",
        "a.b",
    ]


def test_record_fields_returns_empty_on_malformed_shapes() -> None:
    assert extract_record_fields({}) == []
    assert extract_record_fields({"Derived": "x"}) == []
    assert extract_record_fields({"Derived": {"Record": "x"}}) == []
    assert extract_record_fields({"Derived": {"Record": {"Fields": "x"}}}) == []
    # Non-dict field entries are skipped.
    assert extract_record_fields({"Derived": {"Record": {"Fields": ["bad"]}}}) == []


def test_secondary_indexes_skip_primary() -> None:
    indexes = [
        {"IndexName": "pk", "IsPrimary": True},
        {"IndexName": "by_name", "IndexStructure": "BTREE", "SearchKey": [["name"]]},
    ]
    summary = summarize_secondary_indexes(indexes)
    assert len(summary) == 1
    assert summary[0]["indexName"] == "by_name"


# assembled document (I/O via dispatch handler)


def _metadata_handler(
    dataset: dict | None,
    datatype: dict | None,
    indexes: list[dict],
) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        if "`Dataset`" in stmt:
            rows = [dataset] if dataset else []
        elif "`Datatype`" in stmt:
            rows = [datatype] if datatype else []
        else:
            rows = indexes
        return json_response({"status": "success", "results": rows})

    return handler


async def test_run_get_schema_not_found(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_metadata_handler(None, None, []))
    result = await run_get_schema(cap.client, settings, dataverse="DV", dataset="Missing")
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.NOT_FOUND.value


async def test_not_found_suggests_close_dataset_name(settings: Settings) -> None:
    # Dataset lookup misses, but the inventory has a near-miss in the same dataverse.
    inventory = [{"DataverseName": "Yelp", "DatasetName": "YelpUser"}]

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        # The dataset lookup (param-bound) returns nothing; the inventory query
        # (ORDER BY, no params) returns the catalog so a suggestion can form.
        rows = inventory if "ORDER BY" in stmt else []
        return json_response({"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_get_schema(cap.client, settings, dataverse="Yelp", dataset="User")
    assert result.is_error is True
    assert "Did you mean: YelpUser?" in result.structured["errorMessage"]


async def test_not_found_suggests_close_dataverse_name(settings: Settings) -> None:
    # The dataverse itself is a near-miss with no case-insensitive match.
    inventory = [{"DataverseName": "Yelp", "DatasetName": "YelpUser"}]

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        rows = inventory if "ORDER BY" in stmt else []
        return json_response({"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_get_schema(cap.client, settings, dataverse="Ylp", dataset="X")
    assert "Did you mean dataverse: Yelp?" in result.structured["errorMessage"]


async def test_not_found_no_suggestion_when_dataset_unrelated(settings: Settings) -> None:
    # Dataverse resolves, but the dataset name is too far for any suggestion.
    inventory = [{"DataverseName": "Yelp", "DatasetName": "YelpUser"}]

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        rows = inventory if "ORDER BY" in stmt else []
        return json_response({"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_get_schema(cap.client, settings, dataverse="Yelp", dataset="zzzzzz")
    assert result.structured["errorMessage"].endswith("was not found in Metadata.")


async def test_run_get_schema_success_assembles_document(settings: Settings) -> None:
    dataset = {
        "DataverseName": "DV",
        "DatasetName": "Events",
        "DatatypeName": "EventType",
        "DatatypeDataverseName": "DV",
        "DatasetFormat": {"Format": "column"},
        "InternalDetails": {"PrimaryKey": [["eventId"]]},
    }
    datatype = {"Derived": {"Record": {"Fields": [{"FieldName": "eventId", "FieldType": "uuid"}]}}}
    indexes = [{"IndexName": "by_ts", "IndexStructure": "BTREE", "SearchKey": [["ts"]]}]
    cap = make_capturing_cc(settings, handler=_metadata_handler(dataset, datatype, indexes))

    result = await run_get_schema(cap.client, settings, dataverse="DV", dataset="Events")

    assert result.is_error is False
    assert result.structured["primaryKey"] == ["eventId"]
    assert result.structured["datasetFormatInfo"]["format"] == "COLUMNAR"
    assert result.structured["fields"][0]["name"] == "eventId"
    assert result.structured["secondaryIndexes"][0]["indexName"] == "by_ts"
