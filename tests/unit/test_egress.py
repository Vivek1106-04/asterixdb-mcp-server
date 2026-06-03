"""Unit tests for egress layers 1 (timeout string) and 2 (byte ceiling)."""

from __future__ import annotations

import pytest

from asterixdb_mcp.egress import enforce_byte_ceiling, format_timeout
from asterixdb_mcp.errors import ErrorType, GatewayError


def test_format_timeout_renders_ms_suffix() -> None:
    assert format_timeout(30_000) == "30000ms"
    assert format_timeout(1) == "1ms"


def test_format_timeout_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        format_timeout(0)


def test_byte_ceiling_passes_body_within_limit() -> None:
    body = b"x" * 100
    assert enforce_byte_ceiling(body, 100) is body


def test_byte_ceiling_raises_size_limit_when_exceeded() -> None:
    body = b"x" * 101
    with pytest.raises(GatewayError) as exc_info:
        enforce_byte_ceiling(body, 100)
    assert exc_info.value.error_type is ErrorType.SIZE_LIMIT
    # SIZE_LIMIT is not retryable; the same query would return the same oversized body.
    assert exc_info.value.retryable is False


def test_byte_ceiling_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        enforce_byte_ceiling(b"x", 0)


def test_truncate_no_truncation_when_within_limits() -> None:
    from asterixdb_mcp.egress import truncate_for_llm

    rows = [{"i": n} for n in range(3)]
    window, meta = truncate_for_llm(rows, max_rows=10, max_bytes=100_000)
    assert window == rows
    assert meta["truncated"] is False
    assert meta["totalRows"] == 3
    assert meta["deliveredRows"] == 3
    assert "nextStepHint" not in meta


def test_truncate_by_row_count() -> None:
    from asterixdb_mcp.egress import truncate_for_llm

    rows = [{"i": n} for n in range(10)]
    window, meta = truncate_for_llm(rows, max_rows=4, max_bytes=100_000)
    assert len(window) == 4
    assert meta["truncated"] is True
    assert "nextStepHint" in meta


def test_truncate_by_bytes_keeps_at_least_one_row() -> None:
    from asterixdb_mcp.egress import truncate_for_llm

    rows = [{"blob": "x" * 1000} for _ in range(5)]
    window, meta = truncate_for_llm(rows, max_rows=10, max_bytes=10)
    # Byte budget is tiny, but at least one row is always delivered.
    assert len(window) == 1
    assert meta["truncated"] is True


def test_truncate_empty_rows() -> None:
    from asterixdb_mcp.egress import truncate_for_llm

    window, meta = truncate_for_llm([], max_rows=10, max_bytes=100)
    assert window == []
    assert meta["truncated"] is False
    assert meta["totalRows"] == 0


def test_clamp_long_string() -> None:
    from asterixdb_mcp.egress import clamp_long_values

    out = clamp_long_values("x" * 600, 100)
    assert out.startswith("x" * 100)
    assert "[clamped, 600 chars]" in out


def test_clamp_short_string_unchanged() -> None:
    from asterixdb_mcp.egress import clamp_long_values

    assert clamp_long_values("short", 100) == "short"


def test_clamp_recurses_dict_and_list() -> None:
    from asterixdb_mcp.egress import clamp_long_values

    out = clamp_long_values({"a": "y" * 50, "b": ["z" * 50, 1]}, 10)
    assert "[clamped, 50 chars]" in out["a"]
    assert "[clamped, 50 chars]" in out["b"][0]
    assert out["b"][1] == 1


def test_clamp_leaves_non_strings() -> None:
    from asterixdb_mcp.egress import clamp_long_values

    assert clamp_long_values(42, 10) == 42
    assert clamp_long_values(None, 10) is None


def test_bound_rows_clamps_then_caps() -> None:
    from asterixdb_mcp.egress import bound_rows_for_llm

    rows = [{"text": "t" * 1000} for _ in range(5)]
    window, meta = bound_rows_for_llm(rows, max_rows=3, max_bytes=100_000, max_field_chars=50)
    assert len(window) == 3
    assert "[clamped, 1000 chars]" in window[0]["text"]
    assert meta["truncated"] is True
