"""Accuracy test config and the pytest test builder.

A test module declares a list of :class:`AccuracyTestConfig` and calls
:func:`build_accuracy_tests` to turn them into a parametrized pytest test that
runs across every configured model. Each case:

1. spawns the gateway over stdio,
2. runs the prompt through the model with the gateway's tools,
3. scores the tool calls the model made against the expected calls,
4. persists the result and asserts the score meets the threshold.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from .models import Model, get_available_models
from .scorer import calculate_tool_calling_accuracy
from .types import AgentResult, ExpectedToolCall, MockedTool


@dataclass(frozen=True)
class AccuracyTestConfig:
    """One prompt and the tool calls it is expected to elicit."""

    prompt: str | list[str]
    expected_tool_calls: list[ExpectedToolCall]
    system_prompt: str | None = None
    mocked_tools: dict[str, MockedTool] = field(default_factory=dict)
    custom_scorer: Callable[[float, AgentResult], float] | None = None
    validate_result: Callable[[AgentResult], None] | None = None
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def prompts(self) -> list[str]:
        return [self.prompt] if isinstance(self.prompt, str) else list(self.prompt)

    @property
    def description(self) -> str:
        return "\n---\n".join(self.prompts)


def build_accuracy_tests(configs: list[AccuracyTestConfig]) -> Callable[..., None]:
    """Return a parametrized pytest test over ``configs`` x available models."""
    models = get_available_models()
    model_params = models or [None]
    model_ids = [m.display_name for m in models] or ["no-model-configured"]
    config_ids = [_short_id(c.description) for c in configs]

    @pytest.mark.accuracy
    @pytest.mark.parametrize("model", model_params, ids=model_ids)
    @pytest.mark.parametrize("config", configs, ids=config_ids)
    def test_accuracy(config: AccuracyTestConfig, model: Model | None, accuracy_run: Any) -> None:
        if model is None:
            pytest.skip("no LLM API keys configured (set ACCURACY_*_API_KEY)")
        asyncio.run(_run_case(config, model, accuracy_run))

    return test_accuracy


async def _run_case(config: AccuracyTestConfig, model: Model, run: Any) -> None:
    from .mcp_client import McpTestClient

    client = await McpTestClient.initialize(run.cc_base_url, config.extra_env)
    try:
        client.mock_tools(config.mocked_tools)
        result = await model.run(config.prompts, client, extra_system=config.system_prompt)
    finally:
        await client.close()

    score = calculate_tool_calling_accuracy(config.expected_tool_calls, result.tool_calls)
    if config.custom_scorer is not None:
        score = config.custom_scorer(score, result)

    run.storage.save_model_response(
        commit_sha=run.commit_sha,
        run_id=run.run_id,
        prompt=config.description,
        expected_tool_calls=config.expected_tool_calls,
        provider=model.provider,
        requested_model=model.model_name,
        accuracy=score,
        agent_result=result,
    )

    if config.validate_result is not None:
        config.validate_result(result)

    assert score >= run.min_score, (
        f"{model.display_name} scored {score} (< {run.min_score}) on: {config.description}\n"
        f"tool calls made: {[c.tool_name for c in result.tool_calls]}"
    )


def _short_id(description: str) -> str:
    first_line = description.splitlines()[0] if description else ""
    return (first_line[:60] + "…") if len(first_line) > 60 else first_line


ACCURACY_MIN_SCORE = float(os.environ.get("ACCURACY_MIN_SCORE", "0.75"))
