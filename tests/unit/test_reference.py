"""Unit tests for the static reference resources."""

from __future__ import annotations

from asterixdb_mcp.resources.reference import (
    REFERENCE_VERSION,
    read_builtin_functions,
    read_error_codes,
    read_index_types,
    read_query_examples,
    read_query_hints,
    read_sqlpp_syntax,
    read_type_system,
)


def test_sqlpp_syntax() -> None:
    ref = read_sqlpp_syntax()
    assert ref["reference"] == "sqlpp-syntax"
    assert ref["version"] == REFERENCE_VERSION
    assert any("LIMIT" in rule for rule in ref["rules"])
    # Reserved-word backticking guidance must name field names/aliases, not just
    # dataset identifiers (e.g. a `time` field would otherwise fail with ASX1001).
    assert any("`value`" in rule or "`time`" in rule for rule in ref["rules"])


def test_builtin_functions_lists_aggregates() -> None:
    ref = read_builtin_functions()
    names = {f["name"] for f in ref["functions"]}
    assert "stddev_samp" in names


def test_index_types() -> None:
    ref = read_index_types()
    types = {i["type"] for i in ref["indexes"]}
    assert "BTREE" in types


def test_type_system() -> None:
    ref = read_type_system()
    assert "integer" in ref["primitives"]
    assert "MISSING" in ref["unknowns"]


def test_error_codes() -> None:
    ref = read_error_codes()
    codes = {c["code"] for c in ref["codes"]}
    assert "ASX1077" in codes


def test_query_examples() -> None:
    ref = read_query_examples()
    assert ref["examples"]
    assert all("sql" in ex for ex in ref["examples"])


def test_query_hints() -> None:
    ref = read_query_hints()
    assert ref["reference"] == "query-hints"
    assert ref["version"] == REFERENCE_VERSION
    # Each hint must carry the actual /*+ ... */ syntax the agent will write.
    assert all("/*+" in h["syntax"] for h in ref["hints"])
    names = {h["name"] for h in ref["hints"]}
    assert "indexnl" in names
