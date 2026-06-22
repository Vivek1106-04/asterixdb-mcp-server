"""Unit tests for the list_running_queries tool and the admin array reader."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.running_queries import run_list_running_queries
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _array_cc(settings: Settings, body: object, status_code: int = 200) -> object:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return make_capturing_cc(settings, handler=handler)


async def test_lists_running_requests_redacted_by_default(settings: Settings) -> None:
    cap = _array_cc(settings, [{"requestId": "r1", "state": "running"}])
    result = await run_list_running_queries(cap.client, settings)
    assert result.is_error is False
    assert result.structured["count"] == 1
    assert result.structured["statementsRedacted"] is True
    request = cap.requests[-1]
    assert request.url.params.get("redact") == "true"


async def test_include_statements_disables_redaction(settings: Settings) -> None:
    cap = _array_cc(settings, [])
    result = await run_list_running_queries(cap.client, settings, include_statements=True)
    assert result.structured["statementsRedacted"] is False
    assert "No requests" in result.text
    # No redact parameter is sent when statements are requested.
    assert "redact" not in cap.requests[-1].url.params


async def test_non_dict_entries_are_filtered(settings: Settings) -> None:
    cap = _array_cc(settings, [{"requestId": "r1"}, "garbage", 42])
    result = await run_list_running_queries(cap.client, settings)
    assert result.structured["count"] == 1


async def test_summary_mentions_redaction(settings: Settings) -> None:
    cap = _array_cc(settings, [{"requestId": "r1"}])
    result = await run_list_running_queries(cap.client, settings)
    assert "redacted" in result.text


async def test_propagates_transport_error(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_list_running_queries(cap.client, settings)
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INTERNAL.value


async def test_non_array_body_is_an_error(settings: Settings) -> None:
    cap = _array_cc(settings, {"not": "an array"})
    result = await run_list_running_queries(cap.client, settings)
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INTERNAL.value


async def test_non_json_body_is_an_error(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_list_running_queries(cap.client, settings)
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INTERNAL.value


async def test_timeout_is_classified(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_list_running_queries(cap.client, settings)
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.TIMEOUT.value
