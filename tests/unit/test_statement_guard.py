"""Unit tests for statement-level pre-flight guardrails."""

from __future__ import annotations

from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.statement_guard import (
    check_unsupported_functions,
    normalize_statement,
    strip_set_prefix,
)


def test_strip_set_prefix_removes_leading_set() -> None:
    assert strip_set_prefix("SET `compiler.parallelism` '8'; SELECT 1;") == "SELECT 1;"


def test_strip_set_prefix_removes_stacked_set_clauses() -> None:
    assert strip_set_prefix("SET a '1'; SET b '2'; SELECT 1;") == "SELECT 1;"


def test_strip_set_prefix_leaves_plain_statement() -> None:
    assert strip_set_prefix("SELECT 1;") == "SELECT 1;"


def test_normalize_appends_limit_to_unbounded_select() -> None:
    assert normalize_statement("SELECT a FROM x", 20) == "SELECT a FROM x LIMIT 20;"


def test_normalize_keeps_existing_limit() -> None:
    assert normalize_statement("SELECT a FROM x LIMIT 5", 20) == "SELECT a FROM x LIMIT 5;"


def test_normalize_does_not_touch_select_without_from() -> None:
    assert normalize_statement("SELECT 1", 20) == "SELECT 1;"


def test_normalize_strips_set_then_appends_limit() -> None:
    assert normalize_statement("SET a '1'; SELECT b FROM x", 20) == "SELECT b FROM x LIMIT 20;"


def test_normalize_returns_original_when_strip_empties() -> None:
    assert normalize_statement("SET a '1';", 20) == "SET a '1';"


def test_check_flags_stdev() -> None:
    err = check_unsupported_functions("SELECT STDEV(r.stars) FROM x")
    assert err is not None
    assert err.error_type is ErrorType.INVALID_PARAMETER
    assert "STDDEV_SAMP" in err.message


def test_check_flags_bare_stddev() -> None:
    err = check_unsupported_functions("SELECT STDDEV(r.stars) FROM x")
    assert err is not None
    assert "STDDEV_SAMP" in err.message


def test_check_allows_stddev_samp() -> None:
    assert check_unsupported_functions("SELECT STDDEV_SAMP(r.stars) FROM x") is None


def test_check_allows_stddev_pop() -> None:
    assert check_unsupported_functions("SELECT STDDEV_POP(r.stars) FROM x") is None


def test_check_flags_variance() -> None:
    err = check_unsupported_functions("SELECT VARIANCE(r.stars) FROM x")
    assert err is not None
    assert "VAR_SAMP" in err.message


def test_check_is_case_insensitive() -> None:
    assert check_unsupported_functions("select stdev(x) from y") is not None


def test_check_ignores_field_named_like_function() -> None:
    # A field/alias containing the text (no call paren) must not trip the guard.
    assert check_unsupported_functions("SELECT stddev_of_thing FROM x") is None


def test_check_passes_clean_statement() -> None:
    assert check_unsupported_functions("SELECT AVG(stars) FROM x") is None
