"""Egress guardrails.

The gateway caps how much a single query can cost the cluster and the LLM's
context window. Four layers:

Layer 1, query wall-clock (max_time_ms): sent to the CC as its native timeout
parameter. The CC wants a duration string like "30000ms", not an int, so
format_timeout converts it.

Layer 2, response bytes (max_bytes_per_query): maxBytesPerQuery isn't a native
CC parameter, so the gateway enforces it on the buffered response body in
enforce_byte_ceiling.

Layer 3, LIMIT reminder: lives in the tool and prompt descriptions, not in code.
The LLM is told to always include a LIMIT.

Layer 4, rows/bytes delivered to the LLM (max_rows_to_llm / max_bytes_to_llm):
the final guard on the LLM's context window. truncate_for_llm caps the row
window and the serialized byte size of what is handed back, attaching a
truncation metadata block (with a remediation hint) instead of silently
dropping rows.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import ErrorType, GatewayError

# Remediation guidance attached to a truncated result, telling the LLM how to get
# the rest of the data without blowing up its context window.
TRUNCATION_NEXT_STEP_HINT = (
    "Result was truncated to protect the context window. To see more: tighten the WHERE "
    "clause, project only the columns you need, aggregate in SQL++ (GROUP BY), or use "
    "submit_async_query and page through fetch_query_result with offset/limit."
)


def format_timeout(max_time_ms: int) -> str:
    """Render a millisecond ceiling as the CC timeout duration string.

    AsterixDB parses timeout with Duration.parseDurationStringToNanos, so it
    needs a unit suffix like "30000ms".
    """
    if max_time_ms <= 0:
        raise ValueError(f"max_time_ms must be positive, got {max_time_ms}")
    return f"{max_time_ms}ms"


def enforce_byte_ceiling(body: bytes, max_bytes_per_query: int) -> bytes:
    """Reject a CC response body larger than the gateway byte ceiling.

    Returns the body unchanged when it fits. Otherwise raises GatewayError
    (SIZE_LIMIT): we refuse an oversized payload rather than truncate mid-JSON
    and corrupt the envelope.
    """
    if max_bytes_per_query <= 0:
        raise ValueError(f"max_bytes_per_query must be positive, got {max_bytes_per_query}")
    if len(body) > max_bytes_per_query:
        raise GatewayError(
            ErrorType.SIZE_LIMIT,
            f"Response of {len(body)} bytes exceeds the gateway ceiling of "
            f"{max_bytes_per_query} bytes. Add or tighten a LIMIT clause, project fewer "
            f"fields, or push the analysis into SQL++ (GROUP BY / aggregates).",
        )
    return body


def truncate_for_llm(
    rows: list[Any], max_rows: int, max_bytes: int
) -> tuple[list[Any], dict[str, Any]]:
    """Cap a result window by row count and serialized byte size for the LLM.

    Returns the (possibly shortened) rows plus a metadata block describing what
    was delivered. At least one row is always kept when any exist, even if it
    alone exceeds the byte budget, so the caller never gets an empty window from
    a non-empty result. The metadata's ``truncated`` flag and ``nextStepHint``
    let the LLM react instead of assuming it saw everything.
    """
    total_rows = len(rows)
    delivered: list[Any] = []
    delivered_bytes = 0
    for row in rows[: max(max_rows, 0)]:
        encoded = len(json.dumps(row, default=str).encode())
        if delivered and delivered_bytes + encoded > max_bytes:
            break
        delivered.append(row)
        delivered_bytes += encoded

    truncated = len(delivered) < total_rows
    meta: dict[str, Any] = {
        "totalRows": total_rows,
        "deliveredRows": len(delivered),
        "deliveredBytes": delivered_bytes,
        "truncated": truncated,
    }
    if truncated:
        meta["nextStepHint"] = TRUNCATION_NEXT_STEP_HINT
    return delivered, meta


def clamp_long_values(value: Any, max_field_chars: int) -> Any:
    """Recursively clamp any string longer than ``max_field_chars``.

    A single oversized field (a long review ``text``, a giant comma-joined
    ``friends`` list) can dominate a result even when the row count is small. This
    replaces the tail of such a string with a marker noting the original length,
    so the LLM sees a bounded, self-describing value instead of a context bomb.
    Returns new objects; inputs are never mutated.
    """
    if isinstance(value, str):
        if len(value) <= max_field_chars:
            return value
        return value[:max_field_chars] + f"…[clamped, {len(value)} chars]"
    if isinstance(value, dict):
        return {k: clamp_long_values(v, max_field_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [clamp_long_values(item, max_field_chars) for item in value]
    return value


def bound_rows_for_llm(
    rows: list[Any], max_rows: int, max_bytes: int, max_field_chars: int
) -> tuple[list[Any], dict[str, Any]]:
    """Clamp oversized field values, then cap the window by rows and bytes.

    The single egress entry point every results-returning tool uses so no tool
    can hand the LLM an unbounded payload.
    """
    clamped = [clamp_long_values(row, max_field_chars) for row in rows]
    return truncate_for_llm(clamped, max_rows, max_bytes)
