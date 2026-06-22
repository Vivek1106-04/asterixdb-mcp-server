"""Unit tests for the profile_query tool."""

from __future__ import annotations

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.profile_query import run_profile_query
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio

_PROFILE_ENVELOPE = {
    "status": "success",
    "metrics": {"elapsedTime": "12ms"},
    "profile": {
        "job-id": "JID:0",
        "joblets": [
            {
                "node-id": "nc1",
                "tasks": [
                    {"counters": [
                        {"name": "scan", "runtime-id": "ODID:1", "run-time": 8.0,
                         "cardinality-out": 100, "pages-read": 4}
                    ]}
                ],
            }
        ],
    },
}


async def test_returns_runtime_profile_and_metrics(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json=_PROFILE_ENVELOPE)
    result = await run_profile_query(cap.client, settings, statement="SELECT 1;")
    assert result.is_error is False
    assert result.structured["metrics"]["elapsedTime"] == "12ms"
    assert result.structured["profile"]["operators"][0]["operator"] == "scan"
    assert "heaviest is scan" in result.text
    # profile=timings is requested and readonly is forced.
    form = cap.last_query_form()
    assert form["profile"] == "timings"
    assert form["readonly"] == "true"


async def test_enforces_limit_and_reports_effective_statement(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json=_PROFILE_ENVELOPE)
    result = await run_profile_query(
        cap.client, settings, statement="SELECT * FROM DV.Big", limit=5
    )
    # A LIMIT was injected, so the effective statement differs from the input.
    assert "LIMIT 5" in result.structured["effectiveStatement"]


async def test_omits_profile_when_absent(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_profile_query(cap.client, settings, statement="SELECT 1;")
    assert "profile" not in result.structured
    assert "no per-operator" in result.text


async def test_rejects_unsupported_function(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_profile_query(
        cap.client, settings, statement="SELECT STDEV(x) FROM DV.T;"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value


async def test_propagates_execution_error(settings: Settings) -> None:
    envelope = {"status": "fatal", "errors": [{"code": "ASX1001", "msg": "Syntax error"}]}
    cap = make_capturing_cc(settings, response_json=envelope)
    result = await run_profile_query(cap.client, settings, statement="SELEKT 1;")
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.SYNTAX_ERROR.value
