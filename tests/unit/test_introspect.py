"""Unit tests for validate_syntax and explain_query tools."""

from __future__ import annotations

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.introspect import run_explain_query, run_validate_syntax
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


# validate_syntax


async def test_validate_reports_valid_statement(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success"})
    result = await run_validate_syntax(cap.client, settings, statement="SELECT 1;")

    assert result.is_error is False
    assert result.structured["valid"] is True
    assert cap.last_query_form()["compile-only"] == "true"


async def test_validate_classifies_syntax_error(settings: Settings) -> None:
    envelope = {"status": "fatal", "errors": [{"code": "ASX1001", "msg": "Syntax error"}]}
    cap = make_capturing_cc(settings, response_json=envelope)
    result = await run_validate_syntax(cap.client, settings, statement="SELEKT 1;")

    assert result.is_error is False  # invalidity is data, not a tool error
    assert result.structured["valid"] is False
    assert result.structured["errorType"] == ErrorType.SYNTAX_ERROR.value
    assert result.structured["asterixCode"] == "ASX1001"


async def test_validate_classifies_semantic_error(settings: Settings) -> None:
    envelope = {
        "status": "fatal",
        "errors": [{"code": "ASX1077", "msg": "Cannot find dataset Nope"}],
    }
    cap = make_capturing_cc(settings, response_json=envelope)
    result = await run_validate_syntax(cap.client, settings, statement="SELECT * FROM Nope;")

    assert result.structured["valid"] is False
    assert result.structured["errorType"] == ErrorType.SEMANTIC_ERROR.value


async def test_validate_invalid_without_asterix_code(settings: Settings) -> None:
    # An error with no code is classified by message; no asterixCode is surfaced.
    envelope = {"status": "fatal", "errors": [{"msg": "type mismatch in expression"}]}
    cap = make_capturing_cc(settings, response_json=envelope)
    result = await run_validate_syntax(cap.client, settings, statement="SELECT 1+'a';")

    assert result.structured["valid"] is False
    assert result.structured["errorType"] == ErrorType.SEMANTIC_ERROR.value
    assert "asterixCode" not in result.structured


async def test_validate_propagates_transport_error(settings: Settings) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_validate_syntax(cap.client, settings, statement="SELECT 1;")
    assert result.is_error is True


async def test_explain_propagates_transport_error(settings: Settings) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_explain_query(cap.client, settings, statement="SELECT 1;")
    assert result.is_error is True


# explain_query


_PLAN_ENVELOPE = {
    "status": "success",
    "plans": {
        "optimizedLogicalPlan": {
            "operator": "distribute-result",
            "inputs": [
                {
                    "operator": "data-scan",
                    "data-source": "Yelp.Business",
                    "inputs": [],
                }
            ],
        }
    },
}


async def test_explain_returns_structured_plan(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json=_PLAN_ENVELOPE)
    result = await run_explain_query(
        cap.client, settings, statement="SELECT * FROM Yelp.Business LIMIT 1;"
    )

    assert result.is_error is False
    plan = result.structured["plan"]
    assert plan["dataSources"] == ["Yelp.Business"]
    assert plan["operatorCounts"]["data-scan"] == 1
    form = cap.last_query_form()
    assert form["compile-only"] == "true"
    assert form["optimized-logical-plan"] == "true"
    assert form["plan-format"] == "clean_json"


async def test_explain_errors_on_invalid_query(settings: Settings) -> None:
    envelope = {"status": "fatal", "errors": [{"code": "ASX1001", "msg": "Syntax error"}]}
    cap = make_capturing_cc(settings, response_json=envelope)
    result = await run_explain_query(cap.client, settings, statement="SELEKT 1;")

    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.SYNTAX_ERROR.value


async def test_explain_errors_when_no_plan_returned(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "plans": {}})
    result = await run_explain_query(cap.client, settings, statement="SELECT 1;")

    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INTERNAL.value


async def test_explain_summary_text_mentions_depth_and_source(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json=_PLAN_ENVELOPE)
    result = await run_explain_query(cap.client, settings, statement="SELECT 1;")
    assert "Yelp.Business" in result.text
    assert "depth" in result.text
