"""Unit tests for the version and cluster/status resources (success + degraded)."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.resources.cluster_status import read_cluster_status
from asterixdb_mcp.resources.version import read_version
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


async def test_version_success(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"Git revision": "abc1234"})
    payload = await read_version(cap.client)
    assert payload["asterixdb"]["reachable"] is True
    assert payload["asterixdb"]["version"] == "abc1234"
    assert payload["gateway"]["protocolVersion"]


async def test_version_degraded_on_non_json(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    cap = make_capturing_cc(settings, handler=handler)
    payload = await read_version(cap.client)
    assert payload["asterixdb"]["reachable"] is False
    assert payload["asterixdb"]["error"]["errorType"] == "INTERNAL"


async def test_cluster_status_success(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"state": "ACTIVE", "ncs": [], "cc": {}})
    payload = await read_cluster_status(cap.client)
    assert payload["reachable"] is True
    assert payload["state"] == "ACTIVE"


async def test_cluster_status_degraded_on_timeout(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    cap = make_capturing_cc(settings, handler=handler)
    payload = await read_cluster_status(cap.client)
    assert payload["reachable"] is False
    assert payload["error"]["errorType"] == "TIMEOUT"
