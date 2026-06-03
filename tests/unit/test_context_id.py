"""Unit tests for the clientContextID namespace transform."""

from __future__ import annotations

import pytest

from asterixdb_mcp.context_id import (
    EMPTY_SEGMENT_PLACEHOLDER,
    MAX_USER_TAG_LENGTH,
    make_client_context_id,
    parse_client_context_id,
    sanitize_segment,
)


def test_builds_three_segment_id_with_fixed_uuid() -> None:
    # Arrange
    def uuid_factory() -> str:
        return "fixed-uuid-1234"

    # Act
    ccid = make_client_context_id("sess-1", "monthly-rollup", uuid_factory=uuid_factory)

    # Assert
    assert ccid == "sess-1::monthly-rollup::fixed-uuid-1234"
    assert parse_client_context_id(ccid) == ("sess-1", "monthly-rollup", "fixed-uuid-1234")


def test_missing_user_tag_uses_placeholder_segment() -> None:
    ccid = make_client_context_id("sess-1", None, uuid_factory=lambda: "u")
    session, tag, tail = parse_client_context_id(ccid)
    assert tag == EMPTY_SEGMENT_PLACEHOLDER
    assert (session, tail) == ("sess-1", "u")


def test_delimiter_inside_segment_is_sanitized_away() -> None:
    # A user tag containing '::' must not break the three-segment invariant.
    ccid = make_client_context_id("sess::evil", "a::b c", uuid_factory=lambda: "u")
    assert ccid.count("::") == 2  # exactly two delimiters -> three segments
    parse_client_context_id(ccid)  # does not raise


def test_user_tag_is_truncated_to_max_length() -> None:
    long_tag = "x" * (MAX_USER_TAG_LENGTH + 50)
    _, tag, _ = parse_client_context_id(
        make_client_context_id("s", long_tag, uuid_factory=lambda: "u")
    )
    assert len(tag) <= MAX_USER_TAG_LENGTH


def test_sanitize_empty_returns_placeholder() -> None:
    assert sanitize_segment("   ") == EMPTY_SEGMENT_PLACEHOLDER
    assert sanitize_segment("::") == EMPTY_SEGMENT_PLACEHOLDER


def test_parse_rejects_wrong_segment_count() -> None:
    with pytest.raises(ValueError):
        parse_client_context_id("only::two")
    with pytest.raises(ValueError):
        parse_client_context_id("a::b::c::d")
