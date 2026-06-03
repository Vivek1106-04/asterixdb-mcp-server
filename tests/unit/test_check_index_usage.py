"""Unit tests for check_index_usage."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.check_index_usage import run_check_index_usage
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _form(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


def _router(plan: dict, indexes: list[dict]):
    def handler(req: httpx.Request) -> httpx.Response:
        form = _form(req)
        if form.get("compile-only") == "true":
            return httpx.Response(200, json=plan)
        if "Metadata.`Index`" in form.get("statement", ""):
            return httpx.Response(200, json={"status": "success", "results": indexes})
        return httpx.Response(200, json={"status": "success", "results": []})

    return handler


_PLAN_WITH_INDEX = {
    "status": "success",
    "plans": {
        "optimizedLogicalPlan": {
            "operator": "distribute-result",
            "inputs": [
                {
                    "operator": "unnest-map",
                    "data-source": "Sales.Orders.ordersByCity",
                    "inputs": [
                        {"operator": "data-scan", "data-source": "Sales.Orders", "inputs": []}
                    ],
                }
            ],
        }
    },
}


async def test_reports_used_and_unused(settings: Settings) -> None:
    indexes = [
        {"IndexName": "ordersByCity", "IndexStructure": "BTREE", "SearchKey": [["city"]]},
        {"IndexName": "ordersByDate", "IndexStructure": "BTREE", "SearchKey": [["date"]]},
    ]
    cap = make_capturing_cc(settings, handler=_router(_PLAN_WITH_INDEX, indexes))

    result = await run_check_index_usage(
        cap.client, settings, statement="SELECT * FROM Sales.Orders WHERE city='x' LIMIT 5;"
    )

    used = {u["index"] for u in result.structured["used"]}
    unused = {u["index"] for u in result.structured["availableButUnused"]}
    assert used == {"ordersByCity"}
    assert unused == {"ordersByDate"}
    assert result.structured["usesFullScan"] is False


async def test_detects_full_scan(settings: Settings) -> None:
    plan = {
        "status": "success",
        "plans": {
            "optimizedLogicalPlan": {
                "operator": "data-scan",
                "data-source": "Sales.Orders",
                "inputs": [],
            }
        },
    }
    indexes = [{"IndexName": "ordersByDate", "IndexStructure": "BTREE", "SearchKey": []}]
    cap = make_capturing_cc(settings, handler=_router(plan, indexes))

    result = await run_check_index_usage(
        cap.client, settings, statement="SELECT * FROM Sales.Orders LIMIT 5;"
    )

    assert result.structured["usesFullScan"] is True
    assert "full scan" in result.text


async def test_default_dataverse_single_name_source(settings: Settings) -> None:
    plan = {
        "status": "success",
        "plans": {
            "optimizedLogicalPlan": {
                "operator": "data-scan",
                "data-source": "Orders",
                "inputs": [],
            }
        },
    }
    cap = make_capturing_cc(settings, handler=_router(plan, []))
    result = await run_check_index_usage(
        cap.client, settings, statement="SELECT * FROM Orders LIMIT 5;", dataverse="Sales"
    )
    assert result.structured["datasetsAnalyzed"] == [{"dataverse": "Sales", "dataset": "Orders"}]


async def test_source_without_dataverse_is_skipped(settings: Settings) -> None:
    plan = {
        "status": "success",
        "plans": {
            "optimizedLogicalPlan": {"operator": "data-scan", "data-source": "Orders", "inputs": []}
        },
    }
    cap = make_capturing_cc(settings, handler=_router(plan, []))
    result = await run_check_index_usage(
        cap.client, settings, statement="SELECT * FROM Orders LIMIT 5;"
    )
    assert result.structured["datasetsAnalyzed"] == []


async def test_multiple_datasets_each_with_index(settings: Settings) -> None:
    plan = {
        "status": "success",
        "plans": {
            "optimizedLogicalPlan": {
                "operator": "join",
                "inputs": [
                    {"operator": "data-scan", "data-source": "Sales.Orders", "inputs": []},
                    {"operator": "data-scan", "data-source": "Sales.Items", "inputs": []},
                ],
            }
        },
    }
    indexes = [
        {"IndexName": "ix1", "IndexStructure": "BTREE", "SearchKey": []},
        {"IndexName": "ix2", "IndexStructure": "BTREE", "SearchKey": []},
    ]
    cap = make_capturing_cc(settings, handler=_router(plan, indexes))
    result = await run_check_index_usage(
        cap.client,
        settings,
        statement="SELECT * FROM Sales.Orders o JOIN Sales.Items i ON o.id=i.id LIMIT 5;",
    )
    # Two datasets analyzed, each returning two unused indexes.
    assert len(result.structured["datasetsAnalyzed"]) == 2
    assert len(result.structured["availableButUnused"]) == 4


async def test_empty_statement_rejected(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_check_index_usage(cap.client, settings, statement="   ")
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


async def test_unsupported_function_rejected(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_check_index_usage(
        cap.client, settings, statement="SELECT STDEV(x) FROM Orders LIMIT 5;"
    )
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


async def test_compile_error_surfaced(settings: Settings) -> None:
    cap = make_capturing_cc(
        settings,
        response_json={"status": "fatal", "errors": [{"code": "ASX1001", "msg": "Syntax error"}]},
    )
    result = await run_check_index_usage(
        cap.client, settings, statement="SELEKT 1 FROM x LIMIT 1;"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.SYNTAX_ERROR.value


async def test_no_plan_is_internal_error(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "plans": {}})
    result = await run_check_index_usage(
        cap.client, settings, statement="SELECT * FROM x LIMIT 1;"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INTERNAL.value


async def test_malformed_index_rows_are_skipped(settings: Settings) -> None:
    indexes = [{"IndexName": "good", "IndexStructure": "BTREE", "SearchKey": []}, "junk", {"x": 1}]
    cap = make_capturing_cc(settings, handler=_router(_PLAN_WITH_INDEX, indexes))
    result = await run_check_index_usage(
        cap.client, settings, statement="SELECT * FROM Sales.Orders LIMIT 5;"
    )
    all_indexes = result.structured["used"] + result.structured["availableButUnused"]
    assert [i["index"] for i in all_indexes] == ["good"]


async def test_index_query_failure_is_tolerated(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        form = _form(req)
        if form.get("compile-only") == "true":
            return httpx.Response(200, json=_PLAN_WITH_INDEX)
        # Metadata.Index query fails transport-wise.
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_check_index_usage(
        cap.client, settings, statement="SELECT * FROM Sales.Orders LIMIT 5;"
    )
    # Plan parsed fine; index lookup degraded to empty.
    assert result.structured["used"] == []
    assert result.structured["availableButUnused"] == []


async def test_compile_transport_error(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_check_index_usage(
        cap.client, settings, statement="SELECT * FROM x LIMIT 1;"
    )
    assert result.is_error is True
