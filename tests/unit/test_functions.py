"""Unit tests for list_functions and get_function and the builtins catalog."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.builtins_catalog import BUILTINS_BY_NAME, all_builtins
from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.functions import run_get_function, run_list_functions
from tests.conftest import json_response, make_capturing_cc

pytestmark = pytest.mark.anyio


def _split_source_handler(builtins: list[dict] | None, udfs: list[dict] | None):
    """Route the builtin query (function_metadata()) and the UDF query separately."""

    def handler(req: httpx.Request) -> httpx.Response:
        statement = parse_qs(req.content.decode())["statement"][0]
        rows = builtins if "function_metadata()" in statement else udfs
        return json_response({"status": "success", "results": rows or []})

    return handler


# builtins catalog


def test_catalog_is_non_empty_and_indexed() -> None:
    assert len(all_builtins()) > 0
    assert BUILTINS_BY_NAME["stddev_samp"].category == "aggregate"


# list_functions


async def test_list_includes_builtins(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_list_functions(cap.client, settings, name_contains="stddev")
    names = {f["name"] for f in result.structured["functions"]}
    assert "stddev_samp" in names
    assert all(f["language"] == "INTERNAL" for f in result.structured["functions"])


async def test_list_merges_udfs(settings: Settings) -> None:
    udf = {"Name": "my_udf", "Language": "SQLPP", "DataverseName": "Sales", "Arity": "1"}
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": [udf]})
    result = await run_list_functions(cap.client, settings, name_contains="my_udf")
    fns = result.structured["functions"]
    assert len(fns) == 1
    assert fns[0]["language"] == "SQL++"
    assert fns[0]["dataverse"] == "Sales"


async def test_list_language_filter(settings: Settings) -> None:
    udf = {"Name": "jfn", "Language": "JAVA", "DataverseName": "Sales", "Arity": "2"}
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": [udf]})
    result = await run_list_functions(cap.client, settings, language="JAVA")
    assert {f["language"] for f in result.structured["functions"]} == {"JAVA"}


async def test_list_builtins_from_function_metadata(settings: Settings) -> None:
    # Live builtins come from function_metadata(): name/arity/category carry through.
    builtins = [
        {"name": "row_number", "arity": 0, "category": "window", "private": False},
        {"name": "sum", "arity": -1, "category": "aggregate", "private": False},
    ]
    cap = make_capturing_cc(settings, handler=_split_source_handler(builtins, []))
    result = await run_list_functions(cap.client, settings, language="INTERNAL")
    by_name = {f["name"]: f for f in result.structured["functions"]}
    assert by_name["row_number"]["category"] == "window"
    assert by_name["row_number"]["arity"] == 0
    assert by_name["sum"]["category"] == "aggregate"
    assert all(f["language"] == "INTERNAL" for f in result.structured["functions"])


async def test_list_builtin_query_excludes_private(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    await run_list_functions(cap.client, settings, language="INTERNAL")
    statements = [
        parse_qs(r.content.decode())["statement"][0]
        for r in cap.requests
        if r.url.path == "/query/service"
    ]
    assert any("function_metadata()" in s and "private = false" in s for s in statements)


async def test_list_category_filter(settings: Settings) -> None:
    builtins = [
        {"name": "row_number", "arity": 0, "category": "window", "private": False},
        {"name": "sum", "arity": -1, "category": "aggregate", "private": False},
    ]
    cap = make_capturing_cc(settings, handler=_split_source_handler(builtins, []))
    result = await run_list_functions(cap.client, settings, category="window")
    assert {f["name"] for f in result.structured["functions"]} == {"row_number"}


async def test_list_rejects_unknown_category(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_list_functions(cap.client, settings, category="magic")
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


async def test_list_falls_back_to_curated_when_function_metadata_empty(settings: Settings) -> None:
    # function_metadata() yields nothing (or predates the cluster) -> curated catalog.
    cap = make_capturing_cc(settings, handler=_split_source_handler([], []))
    result = await run_list_functions(cap.client, settings, name_contains="stddev")
    assert "stddev_samp" in {f["name"] for f in result.structured["functions"]}


async def test_list_rejects_unknown_language(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_list_functions(cap.client, settings, language="COBOL")
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


async def test_list_pagination(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_list_functions(cap.client, settings, language="INTERNAL", offset=0, limit=3)
    assert len(result.structured["functions"]) == 3
    assert result.structured["moreAvailable"] is True


async def test_list_tolerates_udf_query_failure(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_list_functions(cap.client, settings, language="INTERNAL")
    # Builtins still returned despite the UDF query failing.
    assert result.structured["total"] > 0


async def test_list_skips_malformed_udf_rows(settings: Settings) -> None:
    cap = make_capturing_cc(
        settings, response_json={"status": "success", "results": [{"no_name": 1}, "junk"]}
    )
    result = await run_list_functions(cap.client, settings, language="SQL++")
    assert result.structured["functions"] == []


async def test_list_normalizes_assorted_languages(settings: Settings) -> None:
    rows = [
        {"Name": "a", "DataverseName": "S"},  # Language absent -> non-str path
        {"Name": "b", "Language": "", "DataverseName": "S"},  # empty -> SQL++
        {"Name": "c", "Language": "SCALA", "DataverseName": "S"},  # unknown -> passthrough
    ]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    result = await run_list_functions(cap.client, settings, name_contains="")
    by_name = {f["name"]: f["language"] for f in result.structured["functions"]}
    assert by_name["a"] == "SQL++"
    assert by_name["b"] == "SQL++"
    assert by_name["c"] == "SCALA"


# get_function


async def test_get_builtin(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_get_function(cap.client, settings, name="STDDEV_SAMP")
    assert result.structured["scope"] == "builtin"
    assert result.structured["language"] == "INTERNAL"
    assert cap.requests == []  # no CC call for a builtin


async def test_get_empty_name_rejected(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_get_function(cap.client, settings, name="  ")
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value


async def test_get_udf_with_body(settings: Settings) -> None:
    udf = {
        "Name": "my_udf",
        "DataverseName": "Sales",
        "Arity": "1",
        "Language": "SQLPP",
        "Params": ["x"],
        "ReturnType": "any",
        "Definition": "x + 1",
    }
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": [udf]})
    result = await run_get_function(cap.client, settings, name="my_udf", dataverse="Sales")
    assert result.structured["scope"] == "udf"
    assert result.structured["definition"] == "x + 1"
    assert "safetyWarning" not in result.structured


async def test_get_external_udf_has_safety_warning(settings: Settings) -> None:
    udf = {
        "Name": "jfn",
        "DataverseName": "Sales",
        "Arity": "1",
        "Language": "JAVA",
        "Definition": "com.x.Fn",
    }
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": [udf]})
    result = await run_get_function(cap.client, settings, name="jfn", dataverse="Sales")
    assert "external JAVA UDF" in result.structured["safetyWarning"]
    assert "review it" in result.text.lower()


async def test_get_unknown_is_not_found(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_get_function(cap.client, settings, name="nope", dataverse="Sales")
    assert result.structured["errorType"] == ErrorType.NOT_FOUND.value


async def test_get_non_builtin_without_dataverse_queries_udf(settings: Settings) -> None:
    # Name is not a builtin and no dataverse given -> falls through to a UDF query
    # with no dataverse filter.
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_get_function(cap.client, settings, name="not_a_builtin_fn")
    assert result.structured["errorType"] == ErrorType.NOT_FOUND.value
    assert cap.requests  # a CC query ran
    assert "f.DataverseName" not in cap.last_query_form()["statement"]


async def test_get_udf_transport_error(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_get_function(cap.client, settings, name="my_udf", dataverse="Sales")
    assert result.is_error is True


async def test_get_name_that_is_builtin_but_dataverse_given_queries_udf(settings: Settings) -> None:
    # With a dataverse, even a builtin-looking name resolves as a UDF lookup.
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_get_function(cap.client, settings, name="count", dataverse="Sales")
    assert result.structured["errorType"] == ErrorType.NOT_FOUND.value
    assert cap.requests  # a CC query was made
