"""Unit tests for the explain_physical_plan tool."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.physical_plan import run_explain_physical_plan
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


_JOB_ENVELOPE = {
    "status": "success",
    "plans": {
        "job": {
            "operators": [
                {
                    "id": "ODID:1",
                    "java-class": "x.y.BTreeSearchOperatorDescriptor",
                    "in-arity": 0,
                    "out-arity": 1,
                    "partition-constraints": {"count": 4},
                },
                {
                    "id": "ODID:2",
                    "java-class": "x.y.SortOperatorDescriptor",
                    "in-arity": 1,
                    "out-arity": 1,
                    "partition-constraints": {"count": 2},
                },
            ],
            "connectors": [
                {
                    "in-operator-id": "ODID:1",
                    "out-operator-id": "ODID:2",
                    "connector": {"java-class": "x.y.MToNPartitioningConnectorDescriptor"},
                }
            ],
        }
    },
}


async def test_physical_plan_requests_job_compile_only(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json=_JOB_ENVELOPE)
    result = await run_explain_physical_plan(
        cap.client, settings, statement="SELECT * FROM Yelp.Business LIMIT 1;"
    )

    assert result.is_error is False
    form = cap.last_query_form()
    assert form["compile-only"] == "true"
    assert form["job"] == "true"
    assert form["hyracks-job-format"] == "json"


async def test_physical_plan_summarizes_operators_and_connectors(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json=_JOB_ENVELOPE)
    result = await run_explain_physical_plan(cap.client, settings, statement="SELECT 1;")

    job = result.structured["job"]
    assert job["operatorCount"] == 2
    assert job["operatorCounts"] == {"BTreeSearch": 1, "Sort": 1}
    assert job["connectorCounts"] == {"MToNPartitioning": 1}
    assert job["maxPartitionCount"] == 4
    assert job["connectors"][0] == {
        "kind": "MToNPartitioning",
        "from": "ODID:1",
        "to": "ODID:2",
    }


async def test_physical_plan_summary_text_mentions_parallelism(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json=_JOB_ENVELOPE)
    result = await run_explain_physical_plan(cap.client, settings, statement="SELECT 1;")
    assert "operator(s)" in result.text
    assert "4-way parallel" in result.text


async def test_physical_plan_errors_on_invalid_query(settings: Settings) -> None:
    envelope = {"status": "fatal", "errors": [{"code": "ASX1001", "msg": "Syntax error"}]}
    cap = make_capturing_cc(settings, response_json=envelope)
    result = await run_explain_physical_plan(cap.client, settings, statement="SELEKT 1;")

    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.SYNTAX_ERROR.value


async def test_physical_plan_errors_when_no_job_returned(settings: Settings) -> None:
    # A statement with no runtime job (e.g. pure DDL) has no physical plan.
    cap = make_capturing_cc(settings, response_json={"status": "success", "plans": {}})
    result = await run_explain_physical_plan(cap.client, settings, statement="SELECT 1;")

    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INTERNAL.value


async def test_physical_plan_propagates_transport_error(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_explain_physical_plan(cap.client, settings, statement="SELECT 1;")
    assert result.is_error is True
