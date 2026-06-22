"""Unit tests for the dataset sample-statistics catalog reads."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.sample_stats import (
    DatasetStats,
    fetch_analyzed_datasets,
    fetch_dataset_stats,
    parse_sample_row,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def test_dataset_stats_derives_estimated_size_and_serializes() -> None:
    stats = DatasetStats(
        dataverse="DV",
        dataset="Events",
        row_count=1000,
        avg_item_size_bytes=200,
        sample_target=1063,
    )
    assert stats.estimated_size_bytes == 200000
    assert stats.to_dict() == {
        "rowCountEstimate": 1000,
        "avgItemSizeBytes": 200,
        "estimatedSizeBytes": 200000,
        "sampleTarget": 1063,
    }


def test_parse_sample_row_coerces_missing_numbers_to_zero() -> None:
    parsed = parse_sample_row({"dataverse": "DV", "dataset": "E"})
    assert parsed is not None
    assert parsed.row_count == 0
    assert parsed.avg_item_size_bytes == 0


def test_parse_sample_row_rejects_non_dict() -> None:
    assert parse_sample_row("nope") is None


async def test_fetch_dataset_stats_returns_sample(settings: Settings) -> None:
    row = {"dataverse": "DV", "dataset": "E", "rowCount": 5, "avgItemSize": 10, "sampleTarget": 100}
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": [row]})
    stats = await fetch_dataset_stats(cap.client, "ccid", dataverse="DV", dataset="E")
    assert stats is not None
    assert stats.row_count == 5
    form = cap.last_query_form()
    assert "IndexStructure = 'SAMPLE'" in form["statement"]


async def test_fetch_dataset_stats_returns_none_when_no_sample(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    assert await fetch_dataset_stats(cap.client, "ccid", dataverse="DV", dataset="E") is None


async def test_fetch_dataset_stats_skips_unusable_row(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": ["bad"]})
    assert await fetch_dataset_stats(cap.client, "ccid", dataverse="DV", dataset="E") is None


async def test_fetch_dataset_stats_degrades_on_error(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    assert await fetch_dataset_stats(cap.client, "ccid", dataverse="DV", dataset="E") is None


async def test_fetch_analyzed_datasets_scoped(settings: Settings) -> None:
    rows = [
        {"dataverse": "DV", "dataset": "A"},
        {"dataverse": "DV", "dataset": "B"},
        {"dataverse": 1, "dataset": "bad"},  # non-string keys dropped
        "garbage",  # non-dict row skipped
    ]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    analyzed = await fetch_analyzed_datasets(cap.client, "ccid", dataverse="DV")
    assert analyzed == {("DV", "A"), ("DV", "B")}
    assert "DataverseName = $dv" in cap.last_query_form()["statement"]


async def test_fetch_analyzed_datasets_unscoped(settings: Settings) -> None:
    cap = make_capturing_cc(
        settings,
        response_json={"status": "success", "results": [{"dataverse": "X", "dataset": "Y"}]},
    )
    analyzed = await fetch_analyzed_datasets(cap.client, "ccid")
    assert analyzed == {("X", "Y")}
    assert "$dv" not in cap.last_query_form()["statement"]


async def test_fetch_analyzed_datasets_degrades_on_error(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    assert await fetch_analyzed_datasets(cap.client, "ccid") == set()
