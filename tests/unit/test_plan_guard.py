"""Unit tests for the columnar plan-rejection guardrail."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.plan_guard import check_columnar_scan, enforce_columnar_safety
from asterixdb_mcp.plan_parser import parse_optimized_plan
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _parsed(plan_node: dict):
    return parse_optimized_plan({"optimizedLogicalPlan": plan_node})


_SCAN = {"operator": "data-scan", "data-source": "DV.Cols", "inputs": []}


# check_columnar_scan (pure)


def test_no_columnar_set_is_safe() -> None:
    assert check_columnar_scan(_parsed(_SCAN), set()) is None


def test_no_columnar_dataset_scanned_is_safe() -> None:
    parsed = _parsed(_SCAN)
    assert check_columnar_scan(parsed, {"DV.Other"}) is None


def test_unrestricted_columnar_scan_is_rejected() -> None:
    parsed = _parsed(_SCAN)
    err = check_columnar_scan(parsed, {"DV.Cols"})
    assert err is not None
    assert err.error_type is ErrorType.PLAN_REJECTED
    assert "DV.Cols" in err.message


def test_columnar_scan_with_project_is_safe() -> None:
    plan = {"operator": "project", "inputs": [_SCAN]}
    assert check_columnar_scan(_parsed(plan), {"DV.Cols"}) is None


def test_columnar_scan_with_select_is_safe() -> None:
    plan = {"operator": "select", "condition": "eq($$x,1)", "inputs": [_SCAN]}
    assert check_columnar_scan(_parsed(plan), {"DV.Cols"}) is None


# enforce_columnar_safety (async, with metadata lookup)


def _columnar_record() -> dict:
    return {"DataverseName": "DV", "DatasetName": "Cols", "DatasetFormat": {"Format": "column"}}


async def test_enforce_returns_none_for_unparsable_plan(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    assert await enforce_columnar_safety(cap.client, "c", {}, None) is None


async def test_enforce_returns_none_when_no_datasets(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    plan = {"optimizedLogicalPlan": {"operator": "empty-tuple-source"}}
    assert await enforce_columnar_safety(cap.client, "c", plan, None) is None


async def test_enforce_rejects_columnar_full_scan(settings: Settings) -> None:
    cap = make_capturing_cc(
        settings, response_json={"status": "success", "results": [_columnar_record()]}
    )
    plan = {"optimizedLogicalPlan": _SCAN}
    err = await enforce_columnar_safety(cap.client, "c", plan, None)
    assert err is not None
    assert err.error_type is ErrorType.PLAN_REJECTED


async def test_enforce_allows_row_dataset(settings: Settings) -> None:
    row_record = {"DataverseName": "DV", "DatasetName": "Cols"}  # no DatasetFormat -> ROW
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": [row_record]})
    plan = {"optimizedLogicalPlan": _SCAN}
    assert await enforce_columnar_safety(cap.client, "c", plan, None) is None


async def test_enforce_tolerates_format_query_failure(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    plan = {"optimizedLogicalPlan": _SCAN}
    # Format unknown -> not treated as columnar -> no rejection.
    assert await enforce_columnar_safety(cap.client, "c", plan, None) is None
