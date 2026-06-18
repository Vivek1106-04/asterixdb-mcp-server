"""Unit tests for the shared secondary-index catalog reads."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.index_catalog import (
    SecondaryIndex,
    fetch_indexes_detailed,
    fetch_secondary_indexes,
    normalize_search_key,
    parse_index_detail_row,
    parse_index_row,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def test_normalize_search_key_handles_paths_bare_strings_and_non_lists() -> None:
    assert normalize_search_key([["address", "city"], "sku"]) == ["address.city", "sku"]
    assert normalize_search_key("nope") == []


def test_parse_index_row_rejects_non_dict_and_nameless_rows() -> None:
    assert parse_index_row("junk") is None
    assert parse_index_row({"DatasetName": "Orders"}) is None


def test_parse_index_row_builds_secondary_index() -> None:
    row = {
        "DataverseName": "Shop",
        "DatasetName": "Orders",
        "IndexName": "ix_city",
        "IndexStructure": "BTREE",
        "SearchKey": [["city"]],
    }
    assert parse_index_row(row) == SecondaryIndex(
        "Shop", "Orders", "ix_city", "BTREE", ("city",)
    )


async def test_fetch_scopes_query_to_dataverse(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    await fetch_secondary_indexes(cap.client, "ccid", dataverse="Shop")
    form = {k: v[0] for k, v in parse_qs(cap.requests[-1].content.decode()).items()}
    assert "i.DataverseName = $dv" in form["statement"]
    assert form["$dv"] == '"Shop"'


async def test_fetch_skips_malformed_rows(settings: Settings) -> None:
    rows = [{"IndexName": "good", "IndexStructure": "BTREE", "SearchKey": []}, "junk", {}]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    indexes = await fetch_secondary_indexes(cap.client, "ccid")
    assert [i.name for i in indexes] == ["good"]


async def test_fetch_degrades_to_empty_on_failure(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    assert await fetch_secondary_indexes(cap.client, "ccid") == []


def test_parse_index_detail_row_rejects_non_dict_and_nameless_rows() -> None:
    assert parse_index_detail_row("junk") is None
    assert parse_index_detail_row({"DatasetName": "Orders"}) is None


def test_parse_index_detail_row_coerces_and_omits_optional_fields() -> None:
    # A plain BTREE index: no gram length, full-text config, or array elements.
    detail = parse_index_detail_row(
        {
            "DataverseName": "Shop",
            "DatasetName": "Orders",
            "IndexName": "ix_city",
            "IndexStructure": "BTREE",
            "IsPrimary": False,
            "SearchKey": [["city"]],
            "SearchKeyType": ["string"],
            # A non-list source indicator and a non-int element are both dropped.
            "SearchKeySourceIndicator": "bad",
        }
    )
    assert detail is not None
    out = detail.to_dict()
    assert out["keyFields"] == ["city"]
    assert out["keyFieldTypes"] == ["string"]
    # Absent optionals are omitted entirely, not emitted as null.
    assert "searchKeySourceIndicator" not in out
    assert "gramLength" not in out
    assert "fullTextConfig" not in out
    assert "searchKeyElements" not in out


def test_parse_index_detail_row_drops_non_int_source_indicators() -> None:
    detail = parse_index_detail_row(
        {
            "IndexName": "ix",
            "IndexStructure": "BTREE",
            "SearchKey": [["a"]],
            "SearchKeyType": "notalist",
            "SearchKeySourceIndicator": [0, "x", 1],
        }
    )
    assert detail is not None
    out = detail.to_dict()
    assert out["searchKeySourceIndicator"] == [0, 1]
    assert "keyFieldTypes" not in out


async def test_fetch_detailed_scopes_to_dataset(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    await fetch_indexes_detailed(cap.client, "ccid", dataverse="Shop", dataset="Orders")
    form = {k: v[0] for k, v in parse_qs(cap.requests[-1].content.decode()).items()}
    assert "i.DatasetName = $ds" in form["statement"]
    assert form["$ds"] == '"Orders"'


async def test_fetch_detailed_skips_malformed_rows(settings: Settings) -> None:
    rows = [{"IndexName": "good", "IndexStructure": "BTREE", "SearchKey": []}, "junk"]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    indexes = await fetch_indexes_detailed(cap.client, "ccid", dataverse="Shop")
    assert [i.name for i in indexes] == ["good"]


async def test_fetch_detailed_degrades_to_empty_on_failure(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    assert await fetch_indexes_detailed(cap.client, "ccid", dataverse="Shop") == []
