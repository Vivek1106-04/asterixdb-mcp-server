"""Shared record types for the accuracy harness."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExpectedToolCall:
    """A tool call the model is expected to make for a given prompt.

    ``optional`` marks a call the model *may* make (e.g. a confirming
    ``list_datasets`` before a query) without penalty; a missing required call
    scores the whole prompt 0.
    """

    tool_name: str
    parameters: dict[str, Any] = field(default_factory=dict)
    optional: bool = False


@dataclass(frozen=True)
class LLMToolCall:
    """A tool call the model actually made, as captured by the MCP proxy."""

    tool_call_id: str
    tool_name: str
    parameters: dict[str, Any]


# A mocked tool: given the model's args, return the text the tool would return.
MockedTool = Callable[[dict[str, Any]], "str | Awaitable[str]"]


@dataclass
class AgentResult:
    """Outcome of running one prompt through a model."""

    responding_model: str
    text: str
    tool_calls: list[LLMToolCall]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    response_time_ms: int = 0
