"""Unit tests for recommend_indexes."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.index_catalog import SecondaryIndex
from asterixdb_mcp.plan_parser import parse_plan
from asterixdb_mcp.tools.recommend_indexes import (
    MAX_STATEMENTS,
    _all_predicates,
    _covered_first_keys,
    _index_name,
    extract_fields_from_predicates,
    run_recommend_indexes,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _form(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


def _scan_plan(data_source: str, field: str | None) -> dict:
    """A single-dataset compile envelope with both plans over a full data-scan.

    Mirrors the real CC: the OPTIMIZED plan filters by-INDEX (field name lost),
    while the UNOPTIMIZED `logicalPlan` still filters by-NAME (rendered as the
    expression printer's `AString: {field}` form) — the source of field names.
    """
    scan = {"operator": "data-scan", "data-source": data_source, "inputs": []}
    if field is None:
        opt_root: dict = scan
        unopt_root: dict = scan
    else:
        opt_root = {
            "operator": "select",
            "condition": {"expressions": ['eq(field-access-by-index($$d, 1), "x")']},
            "inputs": [scan],
        }
        unopt_root = {
            "operator": "select",
            "condition": {
                "expressions": [f"eq(field-access-by-name($$d, AString: {{{field}}}), \"x\")"]
            },
            "inputs": [scan],
        }
    return {
        "status": "success",
        "plans": {"optimizedLogicalPlan": opt_root, "logicalPlan": unopt_root},
    }


def _router(plan_by_marker: dict[str, dict], indexes: list[dict]):
    """Route compile POSTs to a plan (matched by a substring of the statement) and
    the metadata index read to the given index rows."""

    def handler(req: httpx.Request) -> httpx.Response:
        form = _form(req)
        statement = form.get("statement", "")
        if "Metadata.`Index`" in statement:
            return httpx.Response(200, json={"status": "success", "results": indexes})
        if form.get("compile-only") == "true":
            for marker, plan in plan_by_marker.items():
                if marker in statement:
                    return httpx.Response(200, json=plan)
        return httpx.Response(200, json={"status": "success", "results": []})

    return handler


# pure helpers


def test_extract_fields_pulls_field_access_names() -> None:
    predicates = (
        'eq(field-access-by-name($$12, "city"), "Austin")',
        'gt(field-access-by-name($$12, "amount"), 100)',
    )
    assert extract_fields_from_predicates(predicates) == ("city", "amount")


def test_extract_fields_ignores_value_literals_and_dedupes() -> None:
    # "Austin" is a value literal, not a field-access name; never a candidate.
    predicates = (
        'eq(field-access-by-name($$1, "city"), "Austin")',
        'eq(field-access-by-name($$1, "city"), "Dallas")',
    )
    assert extract_fields_from_predicates(predicates) == ("city",)


def test_extract_fields_reads_astring_printer_form() -> None:
    # The unoptimized plan renders the field name with the expression printer:
    # field-access-by-name($$var, AString: {fieldName}).
    predicates = (
        "eq(field-access-by-name($$3, AString: {city}), AString: {Austin})",
        "gt(field-access-by-name($$3, AString: {amount}), 100)",
    )
    assert extract_fields_from_predicates(predicates) == ("city", "amount")


def test_extract_fields_empty_when_only_variables() -> None:
    assert extract_fields_from_predicates(("gt($$x, 3)",)) == ()


def test_index_name_sanitizes_and_falls_back() -> None:
    assert _index_name("Orders", "address.city") == "idx_Orders_address_city"
    # A field that sanitizes to nothing falls back to a literal placeholder.
    assert _index_name("Orders", "...") == "idx_Orders_field"


def test_covered_first_keys_uses_only_leading_key() -> None:
    indexes = [
        SecondaryIndex("Shop", "Orders", "ix_ab", "BTREE", ("a", "b")),
        SecondaryIndex("Shop", "Orders", "ix_c", "BTREE", ("c",)),
    ]
    # Composite (a, b) covers a leading filter on `a` but not on `b`.
    assert _covered_first_keys(indexes) == {("Shop", "Orders"): {"a", "c"}}


def test_covered_first_keys_skips_incomplete_index_rows() -> None:
    indexes = [
        SecondaryIndex(None, "Orders", "ix", "BTREE", ("a",)),  # no dataverse
        SecondaryIndex("Shop", "Orders", "ix_empty", "BTREE", ()),  # no key fields
    ]
    assert _covered_first_keys(indexes) == {}


def test_all_predicates_dedupes_across_nodes() -> None:
    plans = {
        "p": {
            "operator": "select",
            "condition": {"expressions": ["eq($$a, 1)"]},
            "inputs": [
                {"operator": "select", "condition": {"expressions": ["eq($$a, 1)"]}, "inputs": []}
            ],
        }
    }
    parsed = parse_plan(plans, "p")
    assert parsed is not None
    assert _all_predicates(parsed) == ("eq($$a, 1)",)


# end-to-end


async def test_recommends_index_for_full_scanned_filtered_field(settings: Settings) -> None:
    cap = make_capturing_cc(
        settings, handler=_router({"Orders": _scan_plan("Shop.Orders", "city")}, [])
    )
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Shop.Orders WHERE city='x' LIMIT 5;"]
    )

    recs = result.structured["recommendations"]
    assert len(recs) == 1
    rec = recs[0]
    assert (rec["dataverse"], rec["dataset"], rec["field"]) == ("Shop", "Orders", "city")
    assert rec["confidence"] == "high"
    assert rec["usesFullScan"] is True
    assert rec["recommendedDDL"] == "CREATE INDEX idx_Orders_city ON Shop.Orders(city);"


async def test_field_already_indexed_is_not_recommended(settings: Settings) -> None:
    indexes = [{"DataverseName": "Shop", "DatasetName": "Orders",
                "IndexName": "ix_city", "IndexStructure": "BTREE", "SearchKey": [["city"]]}]
    cap = make_capturing_cc(
        settings, handler=_router({"Orders": _scan_plan("Shop.Orders", "city")}, indexes)
    )
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Shop.Orders WHERE city='x' LIMIT 5;"]
    )
    assert result.structured["recommendations"] == []
    assert result.structured["indexesKnown"] == 1


async def test_frequency_and_full_scan_drive_ranking(settings: Settings) -> None:
    plans = {
        "Orders": _scan_plan("Shop.Orders", "city"),
        "Items": _scan_plan("Shop.Items", "sku"),
    }
    cap = make_capturing_cc(settings, handler=_router(plans, []))
    # `city` is filtered by two statements, `sku` by one -> city ranks first.
    result = await run_recommend_indexes(
        cap.client,
        settings,
        statements=[
            "SELECT * FROM Shop.Orders WHERE city='a' LIMIT 5;",
            "SELECT * FROM Shop.Orders WHERE city='b' LIMIT 5;",
            "SELECT * FROM Shop.Items WHERE sku='c' LIMIT 5;",
        ],
    )
    recs = result.structured["recommendations"]
    assert [r["field"] for r in recs] == ["city", "sku"]
    assert recs[0]["supportingStatements"] == 2
    assert recs[0]["score"] > recs[1]["score"]


async def test_join_plan_yields_low_confidence_review_entries(settings: Settings) -> None:
    join_plan = {
        "status": "success",
        "plans": {
            "optimizedLogicalPlan": {
                "operator": "join",
                "inputs": [
                    {"operator": "data-scan", "data-source": "Shop.Orders", "inputs": []},
                    {"operator": "data-scan", "data-source": "Shop.Items", "inputs": []},
                ],
            }
        },
    }
    cap = make_capturing_cc(settings, handler=_router({"JOIN": join_plan}, []))
    result = await run_recommend_indexes(
        cap.client,
        settings,
        statements=["SELECT * FROM Shop.Orders o JOIN Shop.Items i ON o.id=i.id /*JOIN*/ LIMIT 5;"],
    )
    recs = result.structured["recommendations"]
    # No field attributable across a join; both datasets surface as review entries.
    assert {r["dataset"] for r in recs} == {"Orders", "Items"}
    assert all(r["field"] is None and r["confidence"] == "low" for r in recs)
    assert all(r["recommendedDDL"] is None for r in recs)


async def test_non_compiling_statement_is_skipped_not_fatal(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        form = _form(req)
        statement = form.get("statement", "")
        if "Metadata.`Index`" in statement:
            return httpx.Response(200, json={"status": "success", "results": []})
        if "GOOD" in statement:
            return httpx.Response(200, json=_scan_plan("Shop.Orders", "city"))
        return httpx.Response(
            200, json={"status": "fatal", "errors": [{"code": "ASX1001", "msg": "Syntax error"}]}
        )

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_recommend_indexes(
        cap.client,
        settings,
        statements=[
            "SELEKT 1 FROM x LIMIT 1;",
            "SELECT * FROM Shop.Orders WHERE city='x' /*GOOD*/ LIMIT 5;",
        ],
    )
    assert result.structured["statementsAnalyzed"] == 1
    assert len(result.structured["recommendations"]) == 1
    assert result.structured["skipped"][0]["errorType"] == ErrorType.SYNTAX_ERROR.value


async def test_empty_statements_rejected(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_recommend_indexes(cap.client, settings, statements=[])
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


async def test_too_many_statements_rejected(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT 1;"] * (MAX_STATEMENTS + 1)
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


async def test_unsupported_function_statement_is_skipped(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_router({}, []))
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT STDEV(x) FROM Shop.Orders LIMIT 5;"]
    )
    assert result.structured["statementsAnalyzed"] == 0
    assert result.structured["skipped"][0]["errorType"] == ErrorType.INVALID_PARAMETER.value


async def test_empty_statement_in_batch_is_skipped(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_router({}, []))
    result = await run_recommend_indexes(cap.client, settings, statements=["   "])
    assert result.structured["statementsAnalyzed"] == 0
    assert result.structured["skipped"][0]["reason"] == "Empty statement."


async def test_long_skipped_statement_preview_is_truncated(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_router({}, []))
    long_stmt = "SELECT STDEV(x) FROM Shop.Orders WHERE " + " AND ".join(
        f"c{i}=1" for i in range(40)
    )
    result = await run_recommend_indexes(cap.client, settings, statements=[long_stmt])
    preview = result.structured["skipped"][0]["statement"]
    assert len(preview) == 120 and preview.endswith("...")


async def test_compile_transport_error_is_skipped(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        form = _form(req)
        if "Metadata.`Index`" in form.get("statement", ""):
            return httpx.Response(200, json={"status": "success", "results": []})
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Shop.Orders WHERE city='x' LIMIT 5;"]
    )
    assert result.structured["statementsAnalyzed"] == 0
    assert len(result.structured["skipped"]) == 1


async def test_no_optimized_plan_is_skipped(settings: Settings) -> None:
    # Compiles cleanly but returns no plan tree (e.g. a non-plan statement).
    cap = make_capturing_cc(
        settings, handler=_router({"Orders": {"status": "success", "plans": {}}}, [])
    )
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT 1 /*Orders*/;"]
    )
    assert result.structured["statementsAnalyzed"] == 0
    assert result.structured["skipped"][0]["errorType"] == ErrorType.INTERNAL.value


async def test_source_without_dataverse_yields_no_recommendations(settings: Settings) -> None:
    # A single-name data-source with no default dataverse cannot be qualified, so
    # the plan contributes no dataset and no recommendation.
    cap = make_capturing_cc(settings, handler=_router({"Orders": _scan_plan("Orders", "city")}, []))
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Orders WHERE city='x' LIMIT 5;"]
    )
    assert result.structured["statementsAnalyzed"] == 1
    assert result.structured["recommendations"] == []
