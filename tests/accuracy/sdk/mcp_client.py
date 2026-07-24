"""MCP client proxy that spawns the gateway over stdio and records tool calls.

Responsibilities:
1. spawn the real ``asterixdb-mcp-server`` as a stdio subprocess,
2. list the tools it advertises and expose them to a provider-agnostic agent,
3. record every tool call (name + parameters) the model makes,
4. allow individual tools to be mocked so a prompt can be scored without a
   live cluster round-trip.

The gateway speaks stdio by default, so no server code changes are needed.
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .types import LLMToolCall, MockedTool


class McpTestClient:
    """Own a live stdio connection to the gateway for the duration of a run."""

    def __init__(self, session: ClientSession, exit_stack: AsyncExitStack) -> None:
        self._session = session
        self._exit_stack = exit_stack
        self._tools: list[Any] = []
        self._mocked: dict[str, MockedTool] = {}
        self._recorded: list[LLMToolCall] = []

    @classmethod
    async def initialize(
        cls, cc_base_url: str, extra_env: dict[str, str] | None = None
    ) -> McpTestClient:
        """Spawn the gateway over stdio pointed at ``cc_base_url``."""
        command = shutil.which("asterixdb-mcp-server")
        if command is not None:
            args: list[str] = []
        else:
            # Fall back to the module entry point in the current interpreter.
            command = sys.executable
            args = ["-m", "asterixdb_mcp.server"]

        env = {
            **os.environ,
            "ASTERIXDB_MCP_TRANSPORT": "stdio",
            "ASTERIXDB_MCP_CC_BASE_URL": cc_base_url,
            **(extra_env or {}),
        }
        server_params = StdioServerParameters(command=command, args=args, env=env)

        exit_stack = AsyncExitStack()
        read, write = await exit_stack.enter_async_context(stdio_client(server_params))
        session = await exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        client = cls(session, exit_stack)
        listed = await session.list_tools()
        client._tools = list(listed.tools)
        return client

    async def close(self) -> None:
        await self._exit_stack.aclose()

    # -- Tool exposure --------------------------------------------------------

    def tool_schemas(self) -> list[dict[str, Any]]:
        """MCP tools as ``{name, description, input_schema}`` records."""
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
            }
            for tool in self._tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Record the call, then run the mock or forward to the gateway.

        Never raises to the agent: a schema/validation failure is returned as
        an error string so the model can course-correct, as tool-calling
        agents do in production.
        """
        self._recorded.append(
            LLMToolCall(tool_call_id=str(uuid.uuid4()), tool_name=name, parameters=arguments)
        )
        try:
            mock = self._mocked.get(name)
            if mock is not None:
                result = mock(arguments)
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[misc]
                return str(result)

            response = await self._session.call_tool(name, arguments)
            return _flatten_tool_result(response)
        except Exception as exc:  # surfaced to the model as an error, not swallowed
            return f'{{"isError": true, "content": {exc!r}}}'

    # -- Recording / mocking --------------------------------------------------

    def mock_tools(self, mocked: dict[str, MockedTool]) -> None:
        self._mocked = dict(mocked)

    def recorded_tool_calls(self) -> list[LLMToolCall]:
        return list(self._recorded)

    def reset(self) -> None:
        self._mocked = {}
        self._recorded = []


def _flatten_tool_result(response: Any) -> str:
    """Collapse an MCP CallToolResult into the text the model should see."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts) if parts else "(no content)"
