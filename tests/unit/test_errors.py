"""Unit tests for the error taxonomy and CC-error classification."""

from __future__ import annotations

from asterixdb_mcp.errors import (
    ErrorType,
    GatewayError,
    classify_cc_error,
    is_retryable,
)


def test_asx_code_maps_to_readonly_violation() -> None:
    err = classify_cc_error(
        asterix_code="ASX1063",
        message="A readonly query cannot contain a non-query statement.",
    )
    assert err.error_type is ErrorType.READONLY_VIOLATION
    assert err.asterix_code == "ASX1063"


def test_syntax_code_maps_to_syntax_error() -> None:
    err = classify_cc_error(asterix_code="ASX1001", message="Syntax error: unexpected token")
    assert err.error_type is ErrorType.SYNTAX_ERROR


def test_message_heuristic_detects_timeout_without_code() -> None:
    err = classify_cc_error(asterix_code=None, message="Query timed out after 30s")
    assert err.error_type is ErrorType.TIMEOUT


def test_unknown_error_falls_back_to_internal() -> None:
    err = classify_cc_error(asterix_code=None, message="something unexpected happened")
    assert err.error_type is ErrorType.INTERNAL


def test_retryable_classification() -> None:
    assert is_retryable(ErrorType.TIMEOUT) is True
    assert is_retryable(ErrorType.PLAN_REJECTED) is True
    assert is_retryable(ErrorType.NOT_READY) is True
    assert is_retryable(ErrorType.READONLY_VIOLATION) is False
    assert is_retryable(ErrorType.SYNTAX_ERROR) is False


def test_to_structured_envelope_shape() -> None:
    err = GatewayError(ErrorType.NOT_FOUND, "no such dataset", asterix_code="ASX0000")
    structured = err.to_structured()
    assert structured == {
        "status": "error",
        "errorType": "NOT_FOUND",
        "errorMessage": "no such dataset",
        "retryable": False,
        "asterixCode": "ASX0000",
    }


def test_taxonomy_has_twelve_codes() -> None:
    # The contract promises a stable 12-code taxonomy shared across all tools.
    assert len(list(ErrorType)) == 12


def test_message_heuristic_readonly_without_code() -> None:
    err = classify_cc_error(asterix_code=None, message="This is a read-only connection")
    assert err.error_type is ErrorType.READONLY_VIOLATION


def test_message_heuristic_syntax_without_code() -> None:
    err = classify_cc_error(asterix_code=None, message="parse error near token")
    assert err.error_type is ErrorType.SYNTAX_ERROR


def test_message_heuristic_semantic_without_code() -> None:
    err = classify_cc_error(asterix_code=None, message="type mismatch in expression")
    assert err.error_type is ErrorType.SEMANTIC_ERROR


def test_to_structured_omits_asterix_code_when_absent() -> None:
    structured = GatewayError(ErrorType.INTERNAL, "boom").to_structured()
    assert "asterixCode" not in structured
