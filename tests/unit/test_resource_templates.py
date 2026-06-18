"""Unit tests for parameterized resource-template readers."""

from __future__ import annotations

import json

import httpx
import pytest

from asterixdb_mcp.cc_client import CCClient
from asterixdb_mcp.config import Settings
from asterixdb_mcp.resources.templates import (
    read_dataset_indexes,
    read_dataset_sample,
    read_dataset_schema,
    read_dataverse_datasets,
    read_dataverse_indexes,
    read_dataverse_schema,
)

pytestmark = pytest.mark.anyio


def _client(handler: object) -> CCClient:
    settings = Settings(cc_base_url="http://test-cc:19002")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.cc_base_url
    )
    return CCClient(settings, http)


def _settings() -> Settings:
    return Settings(cc_base_url="http://test-cc:19002")


def _ok(payload: list[dict]) -> object:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "results": payload})

    return handler


def _err() -> object:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": [{"msg": "boom"}]})

    return handler


async def test_datasets_reader_returns_structured_json() -> None:
    rows = [{"DatasetName": "Orders", "DataverseName": "Sales", "DatasetType": "INTERNAL"}]
    out = await read_dataverse_datasets(_client(_ok(rows)), _settings(), dataverse="Sales")
    parsed = json.loads(out)
    assert parsed["status"] == "success"
    assert parsed["datasets"][0]["dataset"] == "Orders"


async def test_sample_reader_returns_structured_json() -> None:
    sample_rows = [{"id": 1}, {"id": 2}]

    def handler(req: httpx.Request) -> httpx.Response:
        statement = req.content.decode()
        if "Metadata.%60Dataset%60" in statement or "Metadata.`Dataset`" in statement:
            # First the core resolves the dataset by name.
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "results": [{"DatasetName": "Orders", "DataverseName": "Sales"}],
                },
            )
        # Then it pulls the bounded sample.
        return httpx.Response(200, json={"status": "success", "results": sample_rows})

    out = await read_dataset_sample(
        _client(handler), _settings(), dataverse="Sales", dataset="Orders"
    )
    parsed = json.loads(out)
    assert parsed["results"] == sample_rows


async def test_schema_reader_emits_valid_json_on_error_envelope() -> None:
    out = await read_dataset_schema(
        _client(_err()), _settings(), dataverse="Sales", dataset="Orders"
    )
    parsed = json.loads(out)
    # A failing resolution still yields an informative document, never a crash.
    assert "errorType" in parsed


async def test_dataverse_schema_reader_emits_valid_json_on_error_envelope() -> None:
    out = await read_dataverse_schema(_client(_err()), _settings(), dataverse="Sales")
    parsed = json.loads(out)
    assert "errorType" in parsed


async def test_dataset_indexes_reader_projects_full_detail() -> None:
    rows = [
        {
            "IndexName": "ordersByCity",
            "DataverseName": "Sales",
            "DatasetName": "Orders",
            "IndexStructure": "BTREE",
            "IsPrimary": False,
            "IsEnforced": True,
            "SearchKey": [["address", "city"]],
            "SearchKeyType": ["string"],
            "SearchKeySourceIndicator": [0],
            "GramLength": 3,
        }
    ]
    out = await read_dataset_indexes(
        _client(_ok(rows)), _settings(), dataverse="Sales", dataset="Orders"
    )
    parsed = json.loads(out)
    assert parsed["status"] == "success"
    assert parsed["dataset"] == "Orders"
    assert parsed["indexCount"] == 1
    index = parsed["indexes"][0]
    assert index["name"] == "ordersByCity"
    assert index["keyFields"] == ["address.city"]
    assert index["keyFieldTypes"] == ["string"]
    assert index["searchKeySourceIndicator"] == [0]
    assert index["gramLength"] == 3
    assert index["isEnforced"] is True


async def test_dataverse_indexes_reader_returns_empty_catalog_on_error() -> None:
    out = await read_dataverse_indexes(_client(_err()), _settings(), dataverse="Sales")
    parsed = json.loads(out)
    # A transport failure degrades to an empty catalog document, never a crash.
    assert parsed["status"] == "success"
    assert parsed["indexCount"] == 0
    assert parsed["indexes"] == []
