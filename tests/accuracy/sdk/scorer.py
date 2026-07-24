"""Tool-calling accuracy scorer.

The score is one of {0, 0.75, 1}:

- 0    : a required expected tool was never called, or a matched call carried
         wrong/missing parameters.
- 0.75 : the right tools were called, but the model also hallucinated extra
         calls or passed extra parameters.
- 1    : exactly the expected tools with exactly the expected parameters.

For each expected call we greedily pair the best-matching actual call (same
name, highest parameter similarity, >= 0.75). Unpaired *actual* calls cap the
ceiling at 0.75 (hallucination); an unpaired *required* expected call returns 0.
"""

from __future__ import annotations

from .matcher import Matcher
from .types import ExpectedToolCall, LLMToolCall


def calculate_tool_calling_accuracy(
    expected_tool_calls: list[ExpectedToolCall],
    actual_tool_calls: list[LLMToolCall],
) -> float:
    if not expected_tool_calls:
        return 1.0 if not actual_tool_calls else 0.75

    current_score = 0.75 if len(actual_tool_calls) > len(expected_tool_calls) else 1.0
    checked: set[int] = set()

    for expected in expected_tool_calls:
        candidates = []
        for index, call in enumerate(actual_tool_calls):
            if index in checked or call.tool_name != expected.tool_name:
                continue
            score = Matcher.value(expected.parameters).match(call.parameters)
            if score >= 0.75:
                candidates.append((score, index))

        candidates.sort(key=lambda c: (-c[0], c[1]))
        if candidates:
            best_score, best_index = candidates[0]
            checked.add(best_index)
            current_score = min(current_score, best_score)
        elif expected.optional:
            continue
        else:
            return 0.0

    return current_score
