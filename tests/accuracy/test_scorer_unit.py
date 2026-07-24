"""Offline unit tests for the accuracy scorer and matchers.

These need no cluster or API keys, so they run in the default suite and guard
the scoring logic against regressions.
"""

from __future__ import annotations

from .sdk.matcher import MISSING, Matcher
from .sdk.scorer import calculate_tool_calling_accuracy as score
from .sdk.types import ExpectedToolCall, LLMToolCall


def _expected(**params: object) -> list[ExpectedToolCall]:
    return [ExpectedToolCall("execute_query", dict(params))]


def _actual(**params: object) -> list[LLMToolCall]:
    return [LLMToolCall("1", "execute_query", dict(params))]


def test_exact_match_scores_one() -> None:
    exp = _expected(statement=Matcher.contains("mflix_movies"))
    act = _actual(statement="SELECT * FROM accuracy.mflix_movies")
    assert score(exp, act) == 1.0


def test_hallucinated_extra_call_caps_at_075() -> None:
    exp = _expected(statement=Matcher.contains("comics_books"))
    act = [
        LLMToolCall("1", "execute_query", {"statement": "select count(*) from comics_books"}),
        LLMToolCall("2", "list_datasets", {"dataverse": "accuracy"}),
    ]
    assert score(exp, act) == 0.75


def test_missing_required_call_scores_zero() -> None:
    exp = _expected(statement=Matcher.contains("mflix_movies"))
    assert score(exp, []) == 0.0


def test_optional_call_may_be_skipped() -> None:
    exp = [
        ExpectedToolCall("list_datasets", {"dataverse": Matcher.any_value}, optional=True),
        ExpectedToolCall("execute_query", {"statement": Matcher.contains("comics_books")}),
    ]
    act = _actual(statement="select count(*) from accuracy.comics_books")
    assert score(exp, act) == 1.0


def test_wrong_parameter_value_scores_zero() -> None:
    exp = _expected(statement=Matcher.contains("mflix_movies"))
    act = _actual(statement="SELECT * FROM accuracy.other_dataset")
    assert score(exp, act) == 0.0


def test_forbidden_parameter_present_scores_zero() -> None:
    exp = _expected(statement=Matcher.any_value, limit=Matcher.undefined)
    assert score(exp, _actual(statement="x", limit=5)) == 0.0


def test_forbidden_parameter_absent_scores_one() -> None:
    exp = _expected(statement=Matcher.any_value, limit=Matcher.undefined)
    assert score(exp, _actual(statement="x")) == 1.0


def test_empty_expectation_matches_no_calls() -> None:
    assert score([], []) == 1.0
    assert score([], _actual(statement="x")) == 0.75


def test_number_and_string_matchers() -> None:
    assert Matcher.number(lambda v: v > 2).match(5) == 1.0
    assert Matcher.number().match("5") == 0.0
    assert Matcher.string().match("hi") == 1.0
    assert Matcher.case_insensitive("Horror").match("horror") == 1.0
    assert Matcher.any_of(Matcher.undefined, Matcher.number()).match(MISSING) == 1.0
