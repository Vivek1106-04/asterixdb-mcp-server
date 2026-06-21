"""get_reference: SQL++ reference docs as a callable tool, not a passive resource.

A tool-driven LLM never reads MCP resources, so the curated reference material
(syntax rules, type system, index types, error codes, worked examples, built-in
functions) is unreachable when it only lives behind asterixdb://reference/*.
This tool surfaces the same hand-curated data so a model can ground itself
before writing SQL++. The content is static and shared with the resources.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..errors import ErrorType, GatewayError
from ..resources.reference import (
    read_builtin_functions,
    read_error_codes,
    read_index_types,
    read_query_examples,
    read_query_hints,
    read_sqlpp_syntax,
    read_type_system,
)
from . import ToolResult

# Topic -> reader. Order here is the order returned by the "all" topic.
_READERS: dict[str, Callable[[], dict[str, Any]]] = {
    "sqlpp-syntax": read_sqlpp_syntax,
    "type-system": read_type_system,
    "index-types": read_index_types,
    "query-examples": read_query_examples,
    "query-hints": read_query_hints,
    "error-codes": read_error_codes,
    "builtin-functions": read_builtin_functions,
}

VALID_TOPICS: tuple[str, ...] = (*_READERS.keys(), "all")


def run_get_reference(topic: str) -> ToolResult:
    """Return one curated SQL++ reference topic, or every topic when ``topic='all'``."""
    normalized = topic.strip().lower()

    if normalized == "all":
        topics = [reader() for reader in _READERS.values()]
        return ToolResult(
            text=(
                f"All {len(topics)} reference topics: "
                + ", ".join(_READERS.keys())
                + "."
            ),
            structured={"status": "success", "topic": "all", "topics": topics},
        )

    reader = _READERS.get(normalized)
    if reader is None:
        # Layer 2 guard: never raise on bad input. Hand the model a corrective
        # message naming the exact enum values it may choose from.
        valid = ", ".join(VALID_TOPICS)
        err = GatewayError(
            ErrorType.INVALID_PARAMETER,
            f"Unknown reference topic {topic!r}. Valid topics: {valid}.",
        )
        return ToolResult.error(err)

    payload = reader()
    return ToolResult(
        text=f"Reference topic {normalized!r} (version {payload.get('version')}).",
        structured={"status": "success", "topic": normalized, **payload},
    )
