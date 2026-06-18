"""Unit tests for recommend_indexes (native ADVISE primary, heuristic fallback)."""

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
    _index_statements,
    _parse_advise,
    _parse_advise_ddl,
    extract_fields_from_predicates,
    run_recommend_indexes,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio

_DDL_CITY = "CREATE INDEX idx_adv_city ON `Default`.`Shop`.`Orders`(city)"
_DDL_SKU = "CREATE INDEX idx_adv_sku ON `Default`.`Shop`.`Items`(sku)"


def _form(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


def _advise_env(recommended: list[str], current: list[str] | None = None) -> dict:
    """Build a /query/service envelope mirroring the engine's ADVISE result."""
    return {
        "status": "success",
        "results": [
            {
                "#operator": "Advise",
                "advice": {
                    "#operator": "IndexAdvice",
                    "adviseinfo": {
                        "current_indexes": [{"index_statement": c} for c in (current or [])],
                        "recommended_indexes": {
                            "indexes": [{"index_statement": r} for r in recommended]
                        },
                    },
                },
            }
        ],
    }


_SYNTAX_ERR = {"status": "fatal", "errors": [{"code": "ASX1001", "msg": "Syntax error"}]}


def _scan_plan(data_source: str, field: str | None) -> dict:
    """A compile envelope with both plans over a full data-scan (heuristic path)."""
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


def _native_router(advise_by_marker: dict[str, dict], indexes: list[dict] | None = None):
    """Route the index read and ADVISE statements; everything else is empty."""

    def handler(req: httpx.Request) -> httpx.Response:
        statement = _form(req).get("statement", "")
        if "Metadata.`Index`" in statement:
            return httpx.Response(200, json={"status": "success", "results": indexes or []})
        if statement.startswith("ADVISE"):
            for marker, env in advise_by_marker.items():
                if marker in statement:
                    return httpx.Response(200, json=env)
        return httpx.Response(200, json={"status": "success", "results": []})

    return handler


# pure helpers


def test_extract_fields_reads_astring_and_quoted_and_dedupes() -> None:
    predicates = (
        "eq(field-access-by-name($$3, AString: {city}), AString: {Austin})",
        'gt(field-access-by-name($$3, "amount"), 100)',
        "eq(field-access-by-name($$3, AString: {city}), AString: {Dallas})",
    )
    assert extract_fields_from_predicates(predicates) == ("city", "amount")


def test_extract_fields_empty_when_only_variables() -> None:
    assert extract_fields_from_predicates(("gt($$x, 3)",)) == ()


def test_index_name_sanitizes_and_falls_back() -> None:
    assert _index_name("Orders", "address.city") == "idx_Orders_address_city"
    assert _index_name("Orders", "...") == "idx_Orders_field"


def test_covered_first_keys_uses_only_leading_key() -> None:
    indexes = [
        SecondaryIndex("Shop", "Orders", "ix_ab", "BTREE", ("a", "b")),
        SecondaryIndex("Shop", "Orders", "ix_c", "BTREE", ("c",)),
    ]
    assert _covered_first_keys(indexes) == {("Shop", "Orders"): {"a", "c"}}


def test_covered_first_keys_skips_incomplete_index_rows() -> None:
    indexes = [
        SecondaryIndex(None, "Orders", "ix", "BTREE", ("a",)),
        SecondaryIndex("Shop", "Orders", "ix_empty", "BTREE", ()),
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


def test_parse_advise_extracts_recommended_and_current() -> None:
    env = _advise_env(recommended=[_DDL_CITY], current=[_DDL_SKU])
    advice = _parse_advise(env["results"])
    assert advice is not None
    assert advice.recommended == (_DDL_CITY,)
    assert advice.current == (_DDL_SKU,)


def test_parse_advise_returns_none_without_advise_object() -> None:
    # A non-dict row is skipped; with no advise object the result is None.
    assert _parse_advise(["junk", {"n": 1}]) is None
    assert _parse_advise("not a list") is None


def test_index_statements_skips_malformed_entries() -> None:
    entries = [{"index_statement": "CREATE INDEX a"}, "junk", {"x": 1}]
    assert _index_statements(entries) == ("CREATE INDEX a",)
    assert _index_statements(None) == ()


def test_parse_advise_ddl_extracts_location_and_field() -> None:
    assert _parse_advise_ddl(_DDL_CITY) == ("Shop", "Orders", "city")
    assert _parse_advise_ddl("CREATE INDEX weird syntax") == (None, None, None)


# native ADVISE path (end-to-end)


async def test_native_recommends_index(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_native_router({"Orders": _advise_env([_DDL_CITY])}))
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Shop.Orders WHERE city='x' /*Orders*/;"]
    )
    assert result.structured["method"] == "advise"
    rec = result.structured["recommendations"][0]
    assert rec["source"] == "advise"
    assert rec["confidence"] == "high"
    assert rec["recommendedDDL"] == _DDL_CITY + ";"
    assert (rec["dataverse"], rec["dataset"], rec["field"]) == ("Shop", "Orders", "city")


async def test_native_aggregates_support_and_ranks(settings: Settings) -> None:
    handler = _native_router(
        {
            "Q1": _advise_env([_DDL_CITY]),
            "Q2": _advise_env([_DDL_CITY]),
            "Q3": _advise_env([_DDL_SKU]),
        }
    )
    cap = make_capturing_cc(settings, handler=handler)
    result = await run_recommend_indexes(
        cap.client,
        settings,
        statements=[
            "SELECT * FROM Shop.Orders WHERE city='a' /*Q1*/;",
            "SELECT * FROM Shop.Orders WHERE city='b' /*Q2*/;",
            "SELECT * FROM Shop.Items WHERE sku='c' /*Q3*/;",
        ],
    )
    recs = result.structured["recommendations"]
    assert recs[0]["recommendedDDL"] == _DDL_CITY + ";"
    assert recs[0]["supportingStatements"] == 2
    assert recs[1]["supportingStatements"] == 1


async def test_native_surfaces_current_indexes(settings: Settings) -> None:
    handler = _native_router({"Orders": _advise_env([_DDL_CITY], current=[_DDL_SKU])})
    cap = make_capturing_cc(settings, handler=handler)
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Shop.Orders /*Orders*/;"]
    )
    assert result.structured["currentIndexes"] == [_DDL_SKU]


async def test_native_no_recommendation(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_native_router({"Orders": _advise_env([])}))
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Shop.Orders /*Orders*/;"]
    )
    assert result.structured["method"] == "advise"
    assert result.structured["recommendations"] == []
    assert "native ADVISE" in result.text


# heuristic fallback path


def _fallback_handler(
    plan: dict | None, *, indexes: list[dict] | None = None, compile_env: dict | None = None
):
    """ADVISE fails (so the heuristic runs); compile returns the given plan/env."""

    def handler(req: httpx.Request) -> httpx.Response:
        statement = _form(req).get("statement", "")
        if "Metadata.`Index`" in statement:
            return httpx.Response(200, json={"status": "success", "results": indexes or []})
        if statement.startswith("ADVISE"):
            return httpx.Response(200, json=_SYNTAX_ERR)
        if _form(req).get("compile-only") == "true":
            body = compile_env or plan or {"status": "success", "plans": {}}
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"status": "success", "results": []})

    return handler


async def test_advise_error_falls_back_to_heuristic(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_fallback_handler(_scan_plan("Shop.Orders", "city")))
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Shop.Orders WHERE city='x' LIMIT 5;"]
    )
    assert result.structured["method"] == "heuristic"
    rec = result.structured["recommendations"][0]
    assert rec["source"] == "heuristic"
    assert rec["recommendedDDL"] == "CREATE INDEX idx_Orders_city ON Shop.Orders(city);"


async def test_advise_transport_error_falls_back(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        statement = _form(req).get("statement", "")
        if "Metadata.`Index`" in statement:
            return httpx.Response(200, json={"status": "success", "results": []})
        if statement.startswith("ADVISE"):
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=_scan_plan("Shop.Orders", "city"))

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Shop.Orders WHERE city='x' LIMIT 5;"]
    )
    assert result.structured["method"] == "heuristic"
    assert len(result.structured["recommendations"]) == 1


async def test_mixed_native_and_fallback(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        statement = _form(req).get("statement", "")
        if "Metadata.`Index`" in statement:
            return httpx.Response(200, json={"status": "success", "results": []})
        if statement.startswith("ADVISE"):
            if "NATIVE" in statement:
                return httpx.Response(200, json=_advise_env([_DDL_CITY]))
            return httpx.Response(200, json=_SYNTAX_ERR)
        return httpx.Response(200, json=_scan_plan("Shop.Items", "sku"))

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_recommend_indexes(
        cap.client,
        settings,
        statements=[
            "SELECT * FROM Shop.Orders WHERE city='x' /*NATIVE*/;",
            "SELECT * FROM Shop.Items WHERE sku='y' LIMIT 5;",
        ],
    )
    assert result.structured["method"] == "mixed"
    assert result.structured["nativeAdviseStatements"] == 1
    sources = {r["source"] for r in result.structured["recommendations"]}
    assert sources == {"advise", "heuristic"}


async def test_fallback_join_yields_low_confidence_review(settings: Settings) -> None:
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
    cap = make_capturing_cc(settings, handler=_fallback_handler(join_plan))
    result = await run_recommend_indexes(
        cap.client,
        settings,
        statements=["SELECT * FROM Shop.Orders o JOIN Shop.Items i ON o.id=i.id LIMIT 5;"],
    )
    recs = result.structured["recommendations"]
    assert {r["dataset"] for r in recs} == {"Orders", "Items"}
    assert all(r["field"] is None and r["confidence"] == "low" for r in recs)


async def test_fallback_source_without_dataverse_yields_nothing(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_fallback_handler(_scan_plan("Orders", "city")))
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Orders WHERE city='x' LIMIT 5;"]
    )
    assert result.structured["statementsAnalyzed"] == 1
    assert result.structured["recommendations"] == []


async def test_fallback_aggregates_repeated_field(settings: Settings) -> None:
    # Two heuristic statements on the same field reuse one candidate record
    # (accumulating support and de-duplicating the predicate).
    cap = make_capturing_cc(settings, handler=_fallback_handler(_scan_plan("Shop.Orders", "city")))
    result = await run_recommend_indexes(
        cap.client,
        settings,
        statements=[
            "SELECT * FROM Shop.Orders WHERE city='a' LIMIT 5;",
            "SELECT * FROM Shop.Orders WHERE city='b' LIMIT 5;",
        ],
    )
    rec = result.structured["recommendations"][0]
    assert rec["supportingStatements"] == 2
    assert rec["field"] == "city"


async def test_fallback_skips_already_indexed_field(settings: Settings) -> None:
    # The filtered field already leads an existing index -> nothing to recommend
    # (exercises the no-novel-field, not-a-review branch).
    indexes = [
        {"DataverseName": "Shop", "DatasetName": "Orders", "IndexName": "ix_city",
         "IndexStructure": "BTREE", "SearchKey": [["city"]]}
    ]
    cap = make_capturing_cc(
        settings, handler=_fallback_handler(_scan_plan("Shop.Orders", "city"), indexes=indexes)
    )
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Shop.Orders WHERE city='x' LIMIT 5;"]
    )
    assert result.structured["recommendations"] == []


async def test_fallback_compile_error_is_skipped(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_fallback_handler(None, compile_env=_SYNTAX_ERR))
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELEKT 1 FROM x LIMIT 1;"]
    )
    assert result.structured["statementsAnalyzed"] == 0
    assert result.structured["skipped"][0]["errorType"] == ErrorType.SYNTAX_ERROR.value


async def test_fallback_no_optimized_plan_is_skipped(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_fallback_handler(None))
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT 1 LIMIT 1;"]
    )
    assert result.structured["statementsAnalyzed"] == 0
    assert result.structured["skipped"][0]["errorType"] == ErrorType.INTERNAL.value


async def test_fallback_compile_transport_error_is_skipped(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        statement = _form(req).get("statement", "")
        if "Metadata.`Index`" in statement:
            return httpx.Response(200, json={"status": "success", "results": []})
        if statement.startswith("ADVISE"):
            return httpx.Response(200, json=_SYNTAX_ERR)
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT * FROM Shop.Orders LIMIT 5;"]
    )
    assert result.structured["statementsAnalyzed"] == 0
    assert len(result.structured["skipped"]) == 1


# validation + guards


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


async def test_empty_statement_in_batch_is_skipped(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_native_router({}))
    result = await run_recommend_indexes(cap.client, settings, statements=["   "])
    assert result.structured["statementsAnalyzed"] == 0
    assert result.structured["skipped"][0]["reason"] == "Empty statement."


async def test_unsupported_function_statement_is_skipped(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_native_router({}))
    result = await run_recommend_indexes(
        cap.client, settings, statements=["SELECT STDEV(x) FROM Shop.Orders LIMIT 5;"]
    )
    assert result.structured["statementsAnalyzed"] == 0
    assert result.structured["skipped"][0]["errorType"] == ErrorType.INVALID_PARAMETER.value


async def test_long_skipped_statement_preview_is_truncated(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_native_router({}))
    long_stmt = "SELECT STDEV(x) FROM Shop.Orders WHERE " + " AND ".join(
        f"c{i}=1" for i in range(40)
    )
    result = await run_recommend_indexes(cap.client, settings, statements=[long_stmt])
    preview = result.structured["skipped"][0]["statement"]
    assert len(preview) == 120 and preview.endswith("...")
