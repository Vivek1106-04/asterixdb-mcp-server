"""Unit tests for the five power prompts (pure text builders)."""

from __future__ import annotations

from asterixdb_mcp.prompts.power_prompts import (
    compose_analyze_query_performance,
    compose_build_aggregation_query,
    compose_explain_error,
    compose_explore_nested_data,
    compose_recommend_indexes,
)


def test_build_aggregation_with_args() -> None:
    text = compose_build_aggregation_query("DV", "DS", group_by="city", metric="COUNT(*)")
    assert "DV.DS" in text
    assert "city" in text
    assert "COUNT(*)" in text
    assert "Storage-Format Awareness" in text


def test_build_aggregation_without_optional_args() -> None:
    text = compose_build_aggregation_query("DV", "DS")
    assert "<the grouping field>" in text
    assert "<the metric" in text


def test_build_aggregation_avoids_reserved_word_alias() -> None:
    # `value` is reserved in SQL++ (VALUE); using it as an alias makes the
    # scaffolded query fail with ASX1001. The template must use a safe alias.
    text = compose_build_aggregation_query("DV", "DS")
    assert "AS value" not in text
    assert "ORDER BY value" not in text
    assert "AS metric_value" in text


def test_analyze_query_performance_with_statement() -> None:
    text = compose_analyze_query_performance("SELECT 1;")
    assert "SELECT 1;" in text
    assert "metrics" in text


def test_analyze_query_performance_without_statement() -> None:
    text = compose_analyze_query_performance()
    assert "Query under analysis" not in text


def test_recommend_indexes() -> None:
    text = compose_recommend_indexes("DV", "DS")
    assert "check_index_usage" in text
    assert "CREATE INDEX" in text


def test_explore_nested_data() -> None:
    text = compose_explore_nested_data("DV", "DS")
    assert "UNNEST" in text
    assert "Storage-Format Awareness" in text


def test_explain_error() -> None:
    text = compose_explain_error("ASX1077: Cannot find dataset Foo")
    assert "ASX1077" in text
    assert "list_datasets" in text or "search_metadata" in text


def test_prompts_degrade_to_placeholders_without_args() -> None:
    # Every prompt must render usable guidance when invoked with no arguments,
    # so clients that call prompts/get without collecting args don't get an error.
    agg = compose_build_aggregation_query()
    assert "<dataverse>.<dataset>" in agg
    rec = compose_recommend_indexes()
    assert "<dataverse>.<dataset>" in rec
    nested = compose_explore_nested_data()
    assert "<dataverse>.<dataset>" in nested
    err = compose_explain_error()
    assert "<paste the AsterixDB error" in err
