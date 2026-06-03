"""Tool core logic.

Each tool is an SDK-agnostic async run_* function returning a ToolResult. The
MCP/FastMCP binding lives in asterixdb_mcp.server, so tool logic stays fully
unit-testable without standing up an MCP transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import GatewayError


@dataclass(frozen=True)
class ToolResult:
    """An MCP tool result: human-readable text + machine-parseable structured envelope."""

    text: str
    structured: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False

    @classmethod
    def error(cls, err: GatewayError) -> ToolResult:
        """Build an error result from a classified GatewayError."""
        return cls(
            text=f"{err.error_type.value}: {err.message}",
            structured=err.to_structured(),
            is_error=True,
        )
