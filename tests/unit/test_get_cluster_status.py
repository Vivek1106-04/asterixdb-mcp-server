"""Unit tests for the get_cluster_status tool."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.tools.get_cluster_status import run_get_cluster_status
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _handler(version: dict, cluster: dict):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/admin/version":
            return httpx.Response(200, json=version)
        return httpx.Response(200, json=cluster)

    return handler


async def test_returns_version_state_and_nodes(settings: Settings) -> None:
    cap = make_capturing_cc(
        settings,
        handler=_handler(
            {"Git revision": "deadbeef"},
            {"state": "ACTIVE", "ncs": [{"node_id": "asterix_nc1", "state": "ACTIVE"}]},
        ),
    )
    result = await run_get_cluster_status(cap.client)

    assert result.structured["version"] == "deadbeef"
    assert result.structured["state"] == "ACTIVE"
    assert result.structured["nodeCount"] == 1
    assert result.structured["nodes"][0]["nodeId"] == "asterix_nc1"
    assert "asterix_nc1" in result.text


async def test_handles_missing_or_malformed_ncs(settings: Settings) -> None:
    cluster = {"state": "ACTIVE", "ncs": ["junk", {"node_id": "n"}]}
    cap = make_capturing_cc(settings, handler=_handler({"version": "1.0"}, cluster))
    result = await run_get_cluster_status(cap.client)
    assert result.structured["version"] == "1.0"
    assert result.structured["nodeCount"] == 1


async def test_ncs_not_a_list(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler({}, {"state": "X", "ncs": None}))
    result = await run_get_cluster_status(cap.client)
    assert result.structured["nodes"] == []
    assert "none" in result.text


async def test_propagates_transport_error(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_get_cluster_status(cap.client)
    assert result.is_error is True
