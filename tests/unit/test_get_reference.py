"""Unit tests for the get_reference tool."""

from __future__ import annotations

from asterixdb_mcp.tools.get_reference import VALID_TOPICS, run_get_reference


def test_returns_single_topic_with_version() -> None:
    result = run_get_reference("sqlpp-syntax")
    assert result.is_error is False
    assert result.structured["topic"] == "sqlpp-syntax"
    assert result.structured["reference"] == "sqlpp-syntax"
    assert "rules" in result.structured


def test_topic_is_case_insensitive_and_trimmed() -> None:
    result = run_get_reference("  Type-System  ")
    assert result.is_error is False
    assert result.structured["topic"] == "type-system"


def test_all_returns_every_topic() -> None:
    result = run_get_reference("all")
    assert result.is_error is False
    assert result.structured["topic"] == "all"
    # Every curated topic, "all" excluded.
    assert len(result.structured["topics"]) == len(VALID_TOPICS) - 1


def test_query_hints_topic_documents_inline_hints() -> None:
    result = run_get_reference("query-hints")
    assert result.is_error is False
    assert result.structured["reference"] == "query-hints"
    names = {hint["name"] for hint in result.structured["hints"]}
    assert {"indexnl", "hash-bcast", "skip-index"} <= names


def test_unknown_topic_returns_guarded_error_not_exception() -> None:
    result = run_get_reference("syntax")
    assert result.is_error is True
    assert result.structured["errorType"] == "INVALID_PARAMETER"
    # The corrective message must name the valid enum values to self-correct.
    assert "sqlpp-syntax" in result.structured["errorMessage"]
    assert "all" in result.structured["errorMessage"]


def test_every_advertised_topic_resolves() -> None:
    for topic in VALID_TOPICS:
        assert run_get_reference(topic).is_error is False
