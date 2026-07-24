"""Accuracy tests for read queries over the fixture datasets.

Each prompt should drive the model to a single ``execute_query`` against the
right dataset. The optional discovery calls (``list_datasets``, ``get_schema``)
are tolerated but not required.
"""

from __future__ import annotations

from .sdk.matcher import Matcher
from .sdk.runner import AccuracyTestConfig, build_accuracy_tests
from .sdk.types import ExpectedToolCall


def _optional_discovery(dataset: str) -> list[ExpectedToolCall]:
    return [
        ExpectedToolCall("list_dataverses", {}, optional=True),
        ExpectedToolCall("list_datasets", {"dataverse": Matcher.any_value}, optional=True),
        ExpectedToolCall(
            "get_schema",
            {"dataverse": Matcher.any_value, "dataset": dataset},
            optional=True,
        ),
    ]


CONFIGS = [
    AccuracyTestConfig(
        prompt="List all the movies in the accuracy.mflix_movies dataset.",
        expected_tool_calls=[
            *_optional_discovery("mflix_movies"),
            ExpectedToolCall(
                "execute_query",
                {
                    "statement": Matcher.contains("mflix_movies"),
                    "dataverse": Matcher.any_of(Matcher.undefined, Matcher.string()),
                    "offset": Matcher.any_of(Matcher.undefined, Matcher.number()),
                    "limit": Matcher.any_of(Matcher.undefined, Matcher.number()),
                },
            ),
        ],
    ),
    AccuracyTestConfig(
        prompt=(
            "Which movies in accuracy.mflix_movies were directed by 'Christina Collins'? "
            "Give me their titles."
        ),
        expected_tool_calls=[
            *_optional_discovery("mflix_movies"),
            ExpectedToolCall(
                "execute_query",
                {
                    "statement": Matcher.string(
                        lambda s: "mflix_movies" in s.lower() and "christina collins" in s.lower()
                    ),
                    "dataverse": Matcher.any_of(Matcher.undefined, Matcher.string()),
                    "offset": Matcher.any_of(Matcher.undefined, Matcher.number()),
                    "limit": Matcher.any_of(Matcher.undefined, Matcher.number()),
                },
            ),
        ],
        validate_result=lambda r: _assert_contains(r.text, "Human sell"),
    ),
    AccuracyTestConfig(
        prompt="How many comic books are there in the accuracy.comics_books dataset?",
        expected_tool_calls=[
            *_optional_discovery("comics_books"),
            ExpectedToolCall(
                "execute_query",
                {
                    "statement": Matcher.string(
                        lambda s: "comics_books" in s.lower() and "count" in s.lower()
                    ),
                    "dataverse": Matcher.any_of(Matcher.undefined, Matcher.string()),
                    "offset": Matcher.any_of(Matcher.undefined, Matcher.number()),
                    "limit": Matcher.any_of(Matcher.undefined, Matcher.number()),
                },
            ),
        ],
        validate_result=lambda r: _assert_contains(r.text, "40"),
    ),
    AccuracyTestConfig(
        prompt=(
            "From accuracy.comics_characters, list the aliases of every character "
            "flagged as a villain."
        ),
        expected_tool_calls=[
            *_optional_discovery("comics_characters"),
            ExpectedToolCall(
                "execute_query",
                {
                    "statement": Matcher.string(
                        lambda s: "comics_characters" in s.lower() and "villain" in s.lower()
                    ),
                    "dataverse": Matcher.any_of(Matcher.undefined, Matcher.string()),
                    "offset": Matcher.any_of(Matcher.undefined, Matcher.number()),
                    "limit": Matcher.any_of(Matcher.undefined, Matcher.number()),
                },
            ),
        ],
    ),
]


def _assert_contains(text: str, needle: str) -> None:
    assert needle.lower() in (text or "").lower(), f"expected {needle!r} in answer, got: {text!r}"


test_execute_query_accuracy = build_accuracy_tests(CONFIGS)
