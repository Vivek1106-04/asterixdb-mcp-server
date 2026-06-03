"""Unit tests for the list_dataverses tool."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.tools.list_dataverses import run_list_dataverses
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


async def test_lists_dataverses(settings: Settings) -> None:
    rows = [
        {"DataverseName": "Default", "DataFormat": "fmt"},
        {"DataverseName": "Yelp", "DataFormat": "fmt"},
    ]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})

    result = await run_list_dataverses(cap.client, settings)

    assert result.structured["count"] == 2
    assert result.structured["dataverses"][1] == {"dataverse": "Yelp", "dataFormat": "fmt"}
    assert "Yelp" in result.text


async def test_skips_non_dict_rows(settings: Settings) -> None:
    cap = make_capturing_cc(
        settings, response_json={"status": "success", "results": [{"DataverseName": "A"}, "x"]}
    )
    result = await run_list_dataverses(cap.client, settings)
    assert result.structured["count"] == 1


async def test_empty_cluster(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_list_dataverses(cap.client, settings)
    assert result.structured["count"] == 0
    assert "none" in result.text


async def test_propagates_transport_error(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_list_dataverses(cap.client, settings)
    assert result.is_error is True
