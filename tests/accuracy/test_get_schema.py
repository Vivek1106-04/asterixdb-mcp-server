"""Accuracy tests for schema-discovery prompts.

A request to describe a dataset's shape should reach ``get_schema`` for the
right dataverse/dataset, optionally after listing what exists.
"""

from __future__ import annotations

from .sdk.matcher import Matcher
from .sdk.runner import AccuracyTestConfig, build_accuracy_tests
from .sdk.types import ExpectedToolCall

CONFIGS = [
    AccuracyTestConfig(
        prompt="What fields does the accuracy.mflix_movies dataset have?",
        expected_tool_calls=[
            ExpectedToolCall("list_datasets", {"dataverse": Matcher.any_value}, optional=True),
            ExpectedToolCall(
                "get_schema",
                {"dataverse": Matcher.contains("accuracy"), "dataset": "mflix_movies"},
            ),
        ],
    ),
    AccuracyTestConfig(
        prompt="Describe the schema of accuracy.comics_characters.",
        expected_tool_calls=[
            ExpectedToolCall("list_datasets", {"dataverse": Matcher.any_value}, optional=True),
            ExpectedToolCall(
                "get_schema",
                {"dataverse": Matcher.contains("accuracy"), "dataset": "comics_characters"},
            ),
        ],
    ),
    AccuracyTestConfig(
        prompt="Show me 5 sample rows from accuracy.support_tickets.",
        expected_tool_calls=[
            ExpectedToolCall(
                "sample_dataset",
                {
                    "dataverse": Matcher.contains("accuracy"),
                    "dataset": "support_tickets",
                    "size": Matcher.any_of(Matcher.undefined, Matcher.number()),
                    "downloadFormat": Matcher.any_of(Matcher.undefined, Matcher.string()),
                },
            ),
        ],
    ),
]

test_get_schema_accuracy = build_accuracy_tests(CONFIGS)
