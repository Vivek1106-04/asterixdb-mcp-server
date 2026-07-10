"""Unit tests for CCClient transport/parse/envelope error branches."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType, GatewayError
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _raises(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _returns(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_req: httpx.Request) -> httpx.Response:
        return response

    return handler


async def test_execute_timeout_maps_to_timeout(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_raises(httpx.ReadTimeout("slow")))
    with pytest.raises(GatewayError) as exc:
        await cap.client.execute("SELECT 1;", client_context_id="c")
    assert exc.value.error_type is ErrorType.TIMEOUT


async def test_execute_transport_error_maps_to_internal(settings: Settings) -> None:
    # a non-stale, non-timeout transport error goes straight to INTERNAL, no retry
    cap = make_capturing_cc(settings, handler=_raises(httpx.TooManyRedirects("loop")))
    with pytest.raises(GatewayError) as exc:
        await cap.client.execute("SELECT 1;", client_context_id="c")
    assert exc.value.error_type is ErrorType.INTERNAL


async def test_execute_errors_list_is_classified(settings: Settings) -> None:
    body = {"status": "fatal", "errors": [{"code": "ASX1001", "msg": "Syntax error: oops"}]}
    cap = make_capturing_cc(settings, response_json=body)
    with pytest.raises(GatewayError) as exc:
        await cap.client.execute("SELCT 1;", client_context_id="c")
    assert exc.value.error_type is ErrorType.SYNTAX_ERROR


async def test_execute_empty_errors_list_falls_through_to_status_guard(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "fatal", "errors": []})
    with pytest.raises(GatewayError) as exc:
        await cap.client.execute("SELECT 1;", client_context_id="c")
    assert exc.value.error_type is ErrorType.INTERNAL


async def test_execute_nonsuccess_status_maps_to_internal(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "fatal"})
    with pytest.raises(GatewayError) as exc:
        await cap.client.execute("SELECT 1;", client_context_id="c")
    assert exc.value.error_type is ErrorType.INTERNAL


async def test_non_json_body_maps_to_internal(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_returns(httpx.Response(200, content=b"not json")))
    with pytest.raises(GatewayError) as exc:
        await cap.client.execute("SELECT 1;", client_context_id="c")
    assert exc.value.error_type is ErrorType.INTERNAL


async def test_non_object_json_maps_to_internal(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_returns(httpx.Response(200, json=[1, 2, 3])))
    with pytest.raises(GatewayError) as exc:
        await cap.client.execute("SELECT 1;", client_context_id="c")
    assert exc.value.error_type is ErrorType.INTERNAL


async def test_admin_version_success(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"Git revision": "abc"})
    assert await cap.client.admin_version() == {"Git revision": "abc"}


async def test_admin_cluster_success(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"state": "ACTIVE"})
    assert (await cap.client.admin_cluster())["state"] == "ACTIVE"


async def test_admin_timeout_maps_to_timeout(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_raises(httpx.ReadTimeout("slow")))
    with pytest.raises(GatewayError) as exc:
        await cap.client.admin_version()
    assert exc.value.error_type is ErrorType.TIMEOUT


async def test_admin_transport_error_maps_to_internal(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_raises(httpx.ConnectError("refused")))
    with pytest.raises(GatewayError) as exc:
        await cap.client.admin_cluster()
    assert exc.value.error_type is ErrorType.INTERNAL


def _fails_then_succeeds(exc: Exception, response: httpx.Response):
    """First request dies on a stale socket; the retry gets a fresh connection."""
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return response

    return handler, calls


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("reset"), httpx.ReadError("reset"), httpx.RemoteProtocolError("reset")],
)
async def test_stale_connection_is_retried_once(settings: Settings, exc: Exception) -> None:
    ok = httpx.Response(200, json={"status": "success", "results": [1]})
    handler, calls = _fails_then_succeeds(exc, ok)
    cap = make_capturing_cc(settings, handler=handler)
    envelope = await cap.client.execute("SELECT 1;", client_context_id="c")
    assert envelope["results"] == [1]
    assert calls["n"] == 2


async def test_stale_connection_retry_exhausted_maps_to_internal(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_raises(httpx.ReadError("still down")))
    with pytest.raises(GatewayError) as exc:
        await cap.client.execute("SELECT 1;", client_context_id="c")
    assert exc.value.error_type is ErrorType.INTERNAL
    assert "restart" in exc.value.message
