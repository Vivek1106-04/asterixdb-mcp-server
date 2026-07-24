"""Testable LLM models and their tool-calling loops.

A provider-agnostic ``Model`` surface, a registry filtered by which API keys
are present, and a per-provider agent loop that drives the MCP tools exposed by
:class:`McpTestClient`.

Each model is gated on its own env var, so a run exercises exactly the
providers whose keys are configured — no key, no model, no test.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod

from .mcp_client import McpTestClient
from .types import AgentResult

MAX_AGENT_STEPS = 100

SYSTEM_PROMPT = "\n".join(
    [
        "You are an expert assistant with access to tools for an Apache AsterixDB "
        "database, queried with SQL++.",
        "You MUST use the most relevant tool to answer the user's request.",
        "When calling a tool you MUST follow its input schema and provide all required arguments.",
        "If a task needs several tool calls, call them in sequence.",
        "Assume you are already connected to the database; do not attempt to connect.",
        'If you cannot fulfil the request, reply "I don\'t know".',
    ]
)


class Model(ABC):
    provider: str
    model_name: str
    display_name: str

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    async def run(
        self,
        prompts: list[str],
        client: McpTestClient,
        *,
        extra_system: str | None = None,
    ) -> AgentResult: ...


class AnthropicModel(Model):
    provider = "Anthropic"

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.display_name = f"{self.provider} - {model_name}"

    def is_available(self) -> bool:
        return bool(os.environ.get("ACCURACY_ANTHROPIC_API_KEY"))

    async def run(
        self, prompts: list[str], client: McpTestClient, *, extra_system: str | None = None
    ) -> AgentResult:
        import anthropic

        sdk = anthropic.AsyncAnthropic(api_key=os.environ["ACCURACY_ANTHROPIC_API_KEY"])
        tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in client.tool_schemas()
        ]
        system = SYSTEM_PROMPT + (f"\n{extra_system}" if extra_system else "")
        messages: list[dict] = []
        result = AgentResult(responding_model=self.model_name, text="", tool_calls=[])
        started = time.time()

        for prompt in prompts:
            messages.append({"role": "user", "content": prompt})
            for _ in range(MAX_AGENT_STEPS):
                response = await sdk.messages.create(
                    model=self.model_name,
                    max_tokens=2048,
                    system=system,
                    messages=messages,
                    tools=tools,
                )
                result.prompt_tokens += response.usage.input_tokens
                result.completion_tokens += response.usage.output_tokens
                messages.append({"role": "assistant", "content": response.content})

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                for block in response.content:
                    if block.type == "text":
                        result.text += block.text
                if not tool_uses:
                    break

                tool_results = []
                for use in tool_uses:
                    output = await client.call_tool(use.name, dict(use.input))
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": use.id, "content": output}
                    )
                messages.append({"role": "user", "content": tool_results})

        result.tool_calls = client.recorded_tool_calls()
        result.total_tokens = result.prompt_tokens + result.completion_tokens
        result.response_time_ms = int((time.time() - started) * 1000)
        return result


class OpenAIModel(Model):
    provider = "OpenAI"

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.display_name = f"{self.provider} - {model_name}"

    def is_available(self) -> bool:
        return bool(os.environ.get("ACCURACY_OPENAI_API_KEY"))

    async def run(
        self, prompts: list[str], client: McpTestClient, *, extra_system: str | None = None
    ) -> AgentResult:
        import json

        import openai

        sdk = openai.AsyncOpenAI(api_key=os.environ["ACCURACY_OPENAI_API_KEY"])
        tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in client.tool_schemas()
        ]
        system = SYSTEM_PROMPT + (f"\n{extra_system}" if extra_system else "")
        messages: list[dict] = [{"role": "system", "content": system}]
        result = AgentResult(responding_model=self.model_name, text="", tool_calls=[])
        started = time.time()

        for prompt in prompts:
            messages.append({"role": "user", "content": prompt})
            for _ in range(MAX_AGENT_STEPS):
                response = await sdk.chat.completions.create(
                    model=self.model_name, messages=messages, tools=tools
                )
                usage = response.usage
                if usage is not None:
                    result.prompt_tokens += usage.prompt_tokens
                    result.completion_tokens += usage.completion_tokens
                message = response.choices[0].message
                messages.append(message.model_dump(exclude_none=True))

                if message.content:
                    result.text += message.content
                if not message.tool_calls:
                    break

                for call in message.tool_calls:
                    args = json.loads(call.function.arguments or "{}")
                    output = await client.call_tool(call.function.name, args)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

        result.tool_calls = client.recorded_tool_calls()
        result.total_tokens = result.prompt_tokens + result.completion_tokens
        result.response_time_ms = int((time.time() - started) * 1000)
        return result


ALL_TESTABLE_MODELS: list[Model] = [
    AnthropicModel("claude-sonnet-4-5"),
    OpenAIModel("gpt-4o"),
]


def get_available_models() -> list[Model]:
    """Only the models whose API keys are configured in the environment."""
    return [model for model in ALL_TESTABLE_MODELS if model.is_available()]
