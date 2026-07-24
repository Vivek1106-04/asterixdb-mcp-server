# Accuracy tests

End-to-end tool-calling accuracy for the gateway. Each test gives a model a
natural-language prompt, lets it drive the gateway's MCP tools, and scores the
tool calls it made against the calls the prompt should have elicited.

The score for a prompt is one of:

- **1.0** — exactly the expected tools with exactly the expected parameters.
- **0.75** — the right tools, but extra hallucinated calls or extra parameters.
- **0** — a required tool was never called, or a call had wrong parameters.

## Layout

```
tests/accuracy/
  sdk/
    matcher.py     parameter matchers (pin what matters, tolerate the rest)
    scorer.py      the {0, 0.75, 1} scoring rule
    types.py       ExpectedToolCall / LLMToolCall / AgentResult
    mcp_client.py  spawns the gateway over stdio, records every tool call
    models.py      per-provider tool-calling loops (Anthropic, OpenAI)
    runner.py      AccuracyTestConfig + the parametrized pytest builder
    storage.py     writes results to .accuracy/results/<commit>/<runId>.json
    report.py      renders a markdown brief from stored results
  fixtures/
    data/          the fixture datasets (JSON)
    loader.py      creates the `accuracy` dataverse and loads the datasets
  test_*.py        the prompt suites
```

## Running locally

These tests are opt-in — the default `pytest` run deselects them. They need a
live AsterixDB cluster and at least one LLM API key.

```bash
pip install -e '.[dev,accuracy]'

export ACCURACY_ANTHROPIC_API_KEY=sk-...        # and/or ACCURACY_OPENAI_API_KEY
export ACCURACY_CC_BASE_URL=http://localhost:19002
export ACCURACY_LOAD_DATA=1                      # load fixtures at session start

pytest tests/accuracy -m accuracy -v
```

A run exercises exactly the providers whose keys are set — no key, no model.
Results land in `.accuracy/results/`; render a summary with:

```bash
python -m tests.accuracy.sdk.report .accuracy/results/<commit>/<runId>.json
```

## In CI

`.github/workflows/accuracy-tests.yml` runs on demand (`workflow_dispatch`) or
when a pull request gets the `accuracy-tests` label. It boots an AsterixDB
sample cluster, loads the fixtures, runs the suite, uploads the results, and
comments the brief on the PR. Provider API keys come from the
`ACCURACY_ANTHROPIC_API_KEY` / `ACCURACY_OPENAI_API_KEY` repository secrets.

## Adding a test

Declare an `AccuracyTestConfig` and hand your configs to `build_accuracy_tests`:

```python
from .sdk.matcher import Matcher
from .sdk.runner import AccuracyTestConfig, build_accuracy_tests
from .sdk.types import AgentResult, ExpectedToolCall


def _answered_forty(result: AgentResult) -> None:
    assert "40" in result.text, f"expected count 40 in answer, got: {result.text!r}"


CONFIGS = [
    AccuracyTestConfig(
        prompt="How many comic books are in accuracy.comics_books?",
        expected_tool_calls=[
            ExpectedToolCall("list_datasets", {"dataverse": Matcher.any_value}, optional=True),
            ExpectedToolCall("execute_query", {"statement": Matcher.contains("comics_books")}),
        ],
        validate_result=_answered_forty,
    ),
]

test_my_accuracy = build_accuracy_tests(CONFIGS)
```

Mark parameters that must be present with a matcher, forbid a parameter with
`Matcher.undefined`, and mark discovery calls the model *may* make as
`optional=True` so they never penalize the score.
```
