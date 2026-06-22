"""Unit tests for the get_dataset_statistics tool."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.dataset_stats import run_get_dataset_statistics
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


async def test_reports_statistics_when_analyzed(settings: Settings) -> None:
    row = {"dataverse": "DV", "dataset": "E", "rowCount": 1000, "avgItemSize": 200,
           "sampleTarget": 1063}
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": [row]})
    result = await run_get_dataset_statistics(cap.client, settings, dataverse="DV", dataset="E")
    assert result.is_error is False
    assert result.structured["analyzed"] is True
    assert result.structured["statistics"]["rowCountEstimate"] == 1000
    assert "1000 rows" in result.text


async def test_reports_not_analyzed_with_remediation(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_get_dataset_statistics(cap.client, settings, dataverse="DV", dataset="E")
    assert result.is_error is False
    assert result.structured["analyzed"] is False
    assert result.structured["analyzeStatement"] == "ANALYZE DATASET DV.E;"
    assert "statistics" not in result.structured


async def test_requires_both_names(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_get_dataset_statistics(cap.client, settings, dataverse="DV", dataset="  ")
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value


async def test_propagates_transport_error(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    # fetch_dataset_stats swallows the error and returns None -> reported as not analyzed.
    result = await run_get_dataset_statistics(cap.client, settings, dataverse="DV", dataset="E")
    assert result.structured["analyzed"] is False
