"""Unit tests for the compilerParameters allowlist and validation."""

from __future__ import annotations

import pytest

from asterixdb_mcp.compiler_params import (
    ALLOWLIST,
    ParamKind,
    describe_allowlist,
    validate_compiler_parameters,
)
from asterixdb_mcp.errors import ErrorType, GatewayError


def test_empty_params_yield_empty_dict() -> None:
    # Arrange / Act
    result = validate_compiler_parameters({})

    # Assert
    assert result == {}


def test_unknown_key_is_rejected() -> None:
    # Act
    with pytest.raises(GatewayError) as exc_info:
        validate_compiler_parameters({"compiler.notreal": 1})

    # Assert
    assert exc_info.value.error_type is ErrorType.INVALID_PARAMETER
    assert "compiler.notreal" in exc_info.value.message


def test_boolean_accepts_native_bool() -> None:
    assert validate_compiler_parameters({"compiler.cbo": True}) == {"compiler.cbo": "true"}
    assert validate_compiler_parameters({"compiler.cbo": False}) == {"compiler.cbo": "false"}


def test_boolean_accepts_string_form_case_insensitively() -> None:
    assert validate_compiler_parameters({"compiler.cbo": "TRUE"}) == {"compiler.cbo": "true"}
    assert validate_compiler_parameters({"compiler.cbo": " False "}) == {"compiler.cbo": "false"}


def test_boolean_rejects_non_boolean() -> None:
    with pytest.raises(GatewayError) as exc_info:
        validate_compiler_parameters({"compiler.cbo": "maybe"})
    assert exc_info.value.error_type is ErrorType.INVALID_PARAMETER


def test_boolean_rejects_integer() -> None:
    with pytest.raises(GatewayError) as exc_info:
        validate_compiler_parameters({"compiler.cbo": 1})
    assert exc_info.value.error_type is ErrorType.INVALID_PARAMETER


def test_integer_accepts_in_range() -> None:
    assert validate_compiler_parameters({"compiler.parallelism": 8}) == {
        "compiler.parallelism": "8"
    }


def test_integer_accepts_numeric_string() -> None:
    assert validate_compiler_parameters({"compiler.parallelism": " 16 "}) == {
        "compiler.parallelism": "16"
    }


def test_integer_below_minimum_is_rejected() -> None:
    with pytest.raises(GatewayError) as exc_info:
        validate_compiler_parameters({"compiler.parallelism": -1})
    assert exc_info.value.error_type is ErrorType.INVALID_PARAMETER
    assert ">=" in exc_info.value.message


def test_integer_above_maximum_is_rejected() -> None:
    with pytest.raises(GatewayError) as exc_info:
        validate_compiler_parameters({"compiler.parallelism": 999_999})
    assert exc_info.value.error_type is ErrorType.INVALID_PARAMETER
    assert "<=" in exc_info.value.message


def test_integer_rejects_boolean() -> None:
    with pytest.raises(GatewayError) as exc_info:
        validate_compiler_parameters({"compiler.parallelism": True})
    assert exc_info.value.error_type is ErrorType.INVALID_PARAMETER
    assert "boolean" in exc_info.value.message


def test_integer_rejects_non_numeric_string() -> None:
    with pytest.raises(GatewayError) as exc_info:
        validate_compiler_parameters({"compiler.parallelism": "lots"})
    assert exc_info.value.error_type is ErrorType.INVALID_PARAMETER


def test_integer_rejects_unsupported_type() -> None:
    with pytest.raises(GatewayError) as exc_info:
        validate_compiler_parameters({"compiler.parallelism": [1, 2]})
    assert exc_info.value.error_type is ErrorType.INVALID_PARAMETER


def test_bytes_floor_is_enforced() -> None:
    with pytest.raises(GatewayError) as exc_info:
        validate_compiler_parameters({"compiler.sortmemory": 1024})
    assert exc_info.value.error_type is ErrorType.INVALID_PARAMETER


def test_bytes_in_range_is_accepted() -> None:
    budget = 64 * 1024 * 1024
    assert validate_compiler_parameters({"compiler.sortmemory": budget}) == {
        "compiler.sortmemory": str(budget)
    }


def test_multiple_params_validate_together() -> None:
    result = validate_compiler_parameters(
        {"compiler.parallelism": 4, "compiler.cbo": True}
    )
    assert result == {"compiler.parallelism": "4", "compiler.cbo": "true"}


def test_describe_allowlist_covers_every_key() -> None:
    records = describe_allowlist()
    names = {record["name"] for record in records}
    assert names == set(ALLOWLIST)


def test_describe_allowlist_includes_ranges_for_numeric_specs() -> None:
    records = {record["name"]: record for record in describe_allowlist()}
    parallelism = records["compiler.parallelism"]
    assert parallelism["kind"] == ParamKind.INTEGER.value
    assert "minimum" in parallelism
    assert "maximum" in parallelism


def test_describe_allowlist_omits_ranges_for_boolean_specs() -> None:
    records = {record["name"]: record for record in describe_allowlist()}
    cbo = records["compiler.cbo"]
    assert cbo["kind"] == ParamKind.BOOLEAN.value
    assert "minimum" not in cbo
    assert "maximum" not in cbo
