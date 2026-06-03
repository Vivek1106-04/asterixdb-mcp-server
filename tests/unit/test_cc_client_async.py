"""Unit tests for the async-lifecycle and compile methods on CCClient."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType, GatewayError
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


async def test_submit_async_sets_mode_and_returns_handle(settings: Settings) -> None:
    envelope = {
        "status": "running",
        "handle": "/query/service/status/0-1",
        "clientContextID": "sess::_::u",
    }
    cap = make_capturing_cc(settings, response_json=envelope)

    result = await cap.client.submit_async(
        "SELECT * FROM Big LIMIT 5;", client_context_id="sess::_::u"
    )

    assert result["handle"] == "/query/service/status/0-1"
    form = cap.last_query_form()
    assert form["mode"] == "async"
    assert form["readonly"] == "true"


async def test_submit_async_raises_on_compile_error(settings: Settings) -> None:
    envelope = {"status": "fatal", "errors": [{"code": "ASX1001", "msg": "Syntax error"}]}
    cap = make_capturing_cc(settings, response_json=envelope)

    with pytest.raises(GatewayError) as exc_info:
        await cap.client.submit_async("SELEKT 1;", client_context_id="sess::_::u")
    assert exc_info.value.error_type is ErrorType.SYNTAX_ERROR


async def test_compile_query_does_not_raise_on_error(settings: Settings) -> None:
    envelope = {"status": "failed", "errors": [{"code": "ASX1073", "msg": "type mismatch"}]}
    cap = make_capturing_cc(settings, response_json=envelope)

    result = await cap.client.compile_query("SELECT 1;", client_context_id="sess::_::u")

    assert result["errors"][0]["code"] == "ASX1073"
    form = cap.last_query_form()
    assert form["compile-only"] == "true"
    assert "optimized-logical-plan" not in form


async def test_compile_query_requests_plan_when_asked(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "plans": {}})

    await cap.client.compile_query(
        "SELECT 1;", client_context_id="sess::_::u", emit_plan=True
    )

    form = cap.last_query_form()
    assert form["optimized-logical-plan"] == "true"
    assert form["plan-format"] == "clean_json"


async def test_execute_renders_bool_compiler_parameter(settings: Settings) -> None:
    # cc_client is a generic forwarder: a raw bool knob renders to its form string.
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    await cap.client.execute(
        "SELECT 1;", client_context_id="sess::_::u", compiler_parameters={"compiler.cbo": True}
    )
    assert cap.last_query_form()["compiler.cbo"] == "true"


async def test_poll_status_gets_handle(settings: Settings) -> None:
    status_env = {"status": "success", "handle": "/query/service/result/0-1"}
    cap = make_capturing_cc(settings, response_json=status_env)

    result = await cap.client.poll_status("/query/service/status/0-1")

    assert result["handle"] == "/query/service/result/0-1"
    assert cap.requests[-1].method == "GET"
    assert cap.requests[-1].url.path == "/query/service/status/0-1"


async def test_fetch_result_gets_results(settings: Settings) -> None:
    result_env = {"status": "success", "results": [{"x": 1}]}
    cap = make_capturing_cc(settings, response_json=result_env)

    result = await cap.client.fetch_result("/query/service/result/0-1")

    assert result["results"] == [{"x": 1}]
    assert cap.requests[-1].url.path == "/query/service/result/0-1"


async def test_cancel_returns_true_on_ok(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.params["client_context_id"] == "sess::_::u"
        return httpx.Response(200)

    cap = make_capturing_cc(settings, handler=handler)
    assert await cap.client.cancel("sess::_::u") is True


async def test_cancel_raises_not_found_on_404(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=lambda r: httpx.Response(404))
    with pytest.raises(GatewayError) as exc_info:
        await cap.client.cancel("sess::_::u")
    assert exc_info.value.error_type is ErrorType.NOT_FOUND


async def test_cancel_raises_forbidden_on_403(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=lambda r: httpx.Response(403))
    with pytest.raises(GatewayError) as exc_info:
        await cap.client.cancel("sess::_::u")
    assert exc_info.value.error_type is ErrorType.FORBIDDEN


async def test_cancel_raises_internal_on_unexpected(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=lambda r: httpx.Response(500))
    with pytest.raises(GatewayError) as exc_info:
        await cap.client.cancel("sess::_::u")
    assert exc_info.value.error_type is ErrorType.INTERNAL


async def test_cancel_maps_timeout(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    cap = make_capturing_cc(settings, handler=handler)
    with pytest.raises(GatewayError) as exc_info:
        await cap.client.cancel("sess::_::u")
    assert exc_info.value.error_type is ErrorType.TIMEOUT


async def test_cancel_maps_transport_error(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    with pytest.raises(GatewayError) as exc_info:
        await cap.client.cancel("sess::_::u")
    assert exc_info.value.error_type is ErrorType.INTERNAL
