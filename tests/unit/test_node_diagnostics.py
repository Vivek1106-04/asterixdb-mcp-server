"""Unit tests for get_node_details, cluster diagnostics, and CC admin getters."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.resources.cluster_diagnostics import read_cluster_diagnostics
from asterixdb_mcp.resources.dataverses import read_dataverses
from asterixdb_mcp.tools.get_node_details import run_get_node_details
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


# CC admin getters + diagnostics resource


async def test_admin_node_detail_path(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"node_id": "nc1", "heap": 1})
    await cap.client.admin_node_detail("nc1")
    assert cap.requests[-1].url.path == "/admin/cluster/node/nc1"


async def test_admin_diagnostics_path(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"nodes": []})
    await cap.client.admin_diagnostics()
    assert cap.requests[-1].url.path == "/admin/diagnostics"


async def test_read_cluster_diagnostics(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"date": "now", "nodes": [{"id": "nc1"}]})
    result = await read_cluster_diagnostics(cap.client)
    assert result["nodes"] == [{"id": "nc1"}]


# dataverses resource


async def test_read_dataverses(settings: Settings) -> None:
    rows = [
        {"DataverseName": "Default", "DataFormat": "fmt"},
        {"DataverseName": "Sales", "DataFormat": "fmt"},
    ]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    result = await read_dataverses(cap.client, "sess")
    assert result["count"] == 2
    assert result["dataverses"][0] == {"dataverse": "Default", "dataFormat": "fmt"}


async def test_read_dataverses_skips_non_dict(settings: Settings) -> None:
    cap = make_capturing_cc(
        settings, response_json={"status": "success", "results": [{"DataverseName": "A"}, "x"]}
    )
    result = await read_dataverses(cap.client, "sess")
    assert result["count"] == 1


# get_node_details


async def test_node_details_success(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"node_id": "nc1", "heapUsed": 42})
    result = await run_get_node_details(cap.client, settings, node="nc1")
    assert result.structured["node"] == "nc1"
    assert result.structured["details"]["heapUsed"] == 42


async def test_node_details_empty_id(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_get_node_details(cap.client, settings, node="  ")
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


async def test_node_details_rejects_path_traversal(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_get_node_details(cap.client, settings, node="../diagnostics")
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


async def test_node_details_unknown_node_is_not_found(settings: Settings) -> None:
    # A 404 surfaces as a non-JSON body -> INTERNAL, reframed to NOT_FOUND.
    cap = make_capturing_cc(settings, handler=lambda r: httpx.Response(404, text="not found"))
    result = await run_get_node_details(cap.client, settings, node="ncX")
    assert result.structured["errorType"] == ErrorType.NOT_FOUND.value


async def test_node_details_timeout_passthrough(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_get_node_details(cap.client, settings, node="nc1")
    # TIMEOUT is not reframed to NOT_FOUND; it passes through.
    assert result.structured["errorType"] == ErrorType.TIMEOUT.value
