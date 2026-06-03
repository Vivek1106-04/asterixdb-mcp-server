"""Gateway error taxonomy.

A single 12-code enum is shared across every tool so MCP clients can write one
error handler and reuse it. Each error carries an error_type (the stable enum
value surfaced in structuredContent.errorType) and a retryable flag (whether
re-issuing the same request could plausibly succeed).

The gateway maps AsterixDB CC error envelopes onto this taxonomy. The CC stays
the authority on what failed (it returns ASX* codes and messages); the gateway
only sorts the failure into a coarse, LLM-actionable bucket.
"""

from __future__ import annotations

from enum import Enum


class ErrorType(str, Enum):
    """The 12-code gateway error taxonomy (stable across all tools)."""

    TIMEOUT = "TIMEOUT"
    READONLY_VIOLATION = "READONLY_VIOLATION"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    SEMANTIC_ERROR = "SEMANTIC_ERROR"
    SIZE_LIMIT = "SIZE_LIMIT"
    PLAN_REJECTED = "PLAN_REJECTED"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    NOT_READY = "NOT_READY"
    EXPIRED = "EXPIRED"
    INTERNAL = "INTERNAL"


# Error types for which re-issuing the identical request may succeed.
_RETRYABLE: frozenset[ErrorType] = frozenset(
    {ErrorType.TIMEOUT, ErrorType.PLAN_REJECTED, ErrorType.NOT_READY}
)


def is_retryable(error_type: ErrorType) -> bool:
    """Return whether the error type is classified retryable."""
    return error_type in _RETRYABLE


class GatewayError(Exception):
    """An error classified into the gateway taxonomy, ready to render to an MCP client."""

    def __init__(
        self,
        error_type: ErrorType,
        message: str,
        *,
        asterix_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        # The underlying ASX* code from the CC, when one was present.
        self.asterix_code = asterix_code

    @property
    def retryable(self) -> bool:
        return is_retryable(self.error_type)

    def to_structured(self) -> dict[str, object]:
        """Render to the structuredContent error envelope shared by all tools."""
        payload: dict[str, object] = {
            "status": "error",
            "errorType": self.error_type.value,
            "errorMessage": self.message,
            "retryable": self.retryable,
        }
        if self.asterix_code is not None:
            payload["asterixCode"] = self.asterix_code
        return payload


# AsterixDB ASX* error codes that map to a specific gateway error type. Codes not
# listed here fall through to message-based heuristics, then INTERNAL.
#   ASX1063: A readonly query cannot contain a non-query statement.
#   ASX1001/ASX1002: Syntax error / parse failure.
_ASX_CODE_MAP: dict[str, ErrorType] = {
    "ASX1063": ErrorType.READONLY_VIOLATION,
    "ASX1001": ErrorType.SYNTAX_ERROR,
    "ASX1002": ErrorType.SYNTAX_ERROR,
}


def classify_cc_error(
    *,
    asterix_code: str | None,
    message: str,
) -> GatewayError:
    """Classify a single CC error (code + message) into a GatewayError.

    Resolution order: exact ASX* code map first, then case-insensitive message
    keywords, finally ErrorType.INTERNAL as the safe default.
    """
    if asterix_code and asterix_code in _ASX_CODE_MAP:
        error_type = _ASX_CODE_MAP[asterix_code]
        return GatewayError(error_type, message, asterix_code=asterix_code)

    lowered = message.lower()
    if "readonly" in lowered or "read-only" in lowered or "read only" in lowered:
        error_type = ErrorType.READONLY_VIOLATION
    elif "timeout" in lowered or "timed out" in lowered:
        error_type = ErrorType.TIMEOUT
    elif "syntax error" in lowered or "parse error" in lowered:
        error_type = ErrorType.SYNTAX_ERROR
    elif "type mismatch" in lowered or "cannot find" in lowered or "unknown" in lowered:
        error_type = ErrorType.SEMANTIC_ERROR
    else:
        error_type = ErrorType.INTERNAL
    return GatewayError(error_type, message, asterix_code=asterix_code)
