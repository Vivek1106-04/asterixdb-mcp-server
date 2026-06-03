"""Unit tests for the catalog inventory helpers."""

from __future__ import annotations

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.inventory import dataset_names, dataverse_names, fetch_dataset_rows
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


_ROWS = [
    {"DataverseName": "Yelp", "DatasetName": "Review"},
    {"DataverseName": "Yelp", "DatasetName": "YelpUser"},
    {"DataverseName": "TinySocial", "DatasetName": "ChirpMessages"},
]


async def test_fetch_dataset_rows_keeps_only_dicts(settings: Settings) -> None:
    body = {"status": "success", "results": [*_ROWS, "garbage", 42]}
    cap = make_capturing_cc(settings, response_json=body)
    rows = await fetch_dataset_rows(cap.client, ccid="cc")
    assert rows == _ROWS


def test_dataverse_names_distinct_in_first_seen_order() -> None:
    assert dataverse_names(_ROWS) == ["Yelp", "TinySocial"]


def test_dataverse_names_skips_non_string() -> None:
    assert dataverse_names([{"DataverseName": 5}, {"other": "x"}]) == []


def test_dataset_names_filters_by_dataverse() -> None:
    assert dataset_names(_ROWS, "Yelp") == ["Review", "YelpUser"]


def test_dataset_names_skips_non_string_dataset() -> None:
    rows = [{"DataverseName": "Yelp", "DatasetName": 9}]
    assert dataset_names(rows, "Yelp") == []
