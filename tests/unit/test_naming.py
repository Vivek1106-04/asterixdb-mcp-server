"""Unit tests for catalog name resolution."""

from __future__ import annotations

import pytest

from asterixdb_mcp.errors import ErrorType, GatewayError
from asterixdb_mcp.naming import quote_identifier, resolve


def test_quote_identifier_wraps_valid_name() -> None:
    assert quote_identifier("Business") == "`Business`"
    assert quote_identifier("review_count_2") == "`review_count_2`"


def test_quote_identifier_rejects_unsafe_name() -> None:
    with pytest.raises(GatewayError) as exc:
        quote_identifier("a`; DROP")
    assert exc.value.error_type is ErrorType.INVALID_PARAMETER


def test_exact_match_returns_canonical_without_suggestions() -> None:
    canonical, suggestions = resolve("Yelp", ["Yelp", "TinySocial"])
    assert canonical == "Yelp"
    assert suggestions == []


def test_case_insensitive_match_returns_canonical() -> None:
    canonical, suggestions = resolve("yelp", ["Yelp", "TinySocial"])
    assert canonical == "Yelp"
    assert suggestions == []


def test_close_miss_returns_suggestions() -> None:
    canonical, suggestions = resolve("YelpUsr", ["YelpUser", "Review"])
    assert canonical is None
    assert "YelpUser" in suggestions


def test_unrelated_miss_returns_no_suggestions() -> None:
    canonical, suggestions = resolve("zzzzzz", ["Yelp", "Review"])
    assert canonical is None
    assert suggestions == []
