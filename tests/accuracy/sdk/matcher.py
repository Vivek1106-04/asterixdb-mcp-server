"""Parameter matchers for tool-call accuracy scoring.

``match`` returns a similarity in [0, 1]; the scorer treats >= 0.75 as a real
match. Matchers let a test pin the parameters that matter (a table name, a
filter) while tolerating the ones that legitimately vary (an optional limit).

JSON tool arguments have no ``undefined``; an argument the model omitted arrives
as the ``MISSING`` sentinel so ``Matcher.undefined`` can forbid a parameter
distinctly from an explicit ``null``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class _Missing:
    """Sentinel for a tool argument the model did not provide at all."""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "<MISSING>"


MISSING = _Missing()


class Matcher:
    """Base matcher. Subclasses return a similarity score in [0, 1]."""

    def match(self, actual: Any) -> float:
        raise NotImplementedError

    # -- Factory helpers for building matchers in test definitions. -----------

    empty_object_or_undefined: Matcher
    any_value: Matcher
    undefined: Matcher
    null: Matcher

    @staticmethod
    def number(additional_filter: Callable[[float], bool] | None = None) -> Matcher:
        return _NumberMatcher(additional_filter or (lambda _v: True))

    @staticmethod
    def string(additional_filter: Callable[[str], bool] | None = None) -> Matcher:
        return _StringMatcher(additional_filter or (lambda _v: True))

    @staticmethod
    def boolean(expected: bool | None = None) -> Matcher:
        return _BooleanMatcher(expected)

    @staticmethod
    def any_of(*matchers: Matcher) -> Matcher:
        return _CompositeMatcher(list(matchers))

    @staticmethod
    def not_(matcher: Matcher) -> Matcher:
        return _NotMatcher(matcher)

    @staticmethod
    def case_insensitive(text: str) -> Matcher:
        return _CaseInsensitiveStringMatcher(text)

    @staticmethod
    def array_or_single(matcher: Matcher) -> Matcher:
        return _ArrayOrSingleMatcher(matcher)

    @staticmethod
    def contains(substring: str, *, case_insensitive: bool = True) -> Matcher:
        """String matcher asserting the value contains ``substring``.

        The natural way to pin a SQL++ statement without demanding an exact
        string: require the discriminating table/keyword be present.
        """
        needle = substring.lower() if case_insensitive else substring
        return _StringMatcher(lambda v: needle in (v.lower() if case_insensitive else v))

    @staticmethod
    def value(expected: Any) -> Matcher:
        if isinstance(expected, Matcher):
            return expected
        return _ValueMatcher(expected)


class _EmptyObjectOrUndefinedMatcher(Matcher):
    def match(self, actual: Any) -> float:
        if actual is MISSING or actual is None:
            return 1.0
        if isinstance(actual, dict) and len(actual) == 0:
            return 1.0
        return 0.0


class _AnyValueMatcher(Matcher):
    def match(self, actual: Any) -> float:
        return 1.0


class _UndefinedMatcher(Matcher):
    def match(self, actual: Any) -> float:
        return 1.0 if actual is MISSING else 0.0


class _NullMatcher(Matcher):
    def match(self, actual: Any) -> float:
        return 1.0 if actual is None else 0.0


class _NumberMatcher(Matcher):
    def __init__(self, additional_filter: Callable[[float], bool]) -> None:
        self._filter = additional_filter

    def match(self, actual: Any) -> float:
        ok = isinstance(actual, (int, float)) and not isinstance(actual, bool)
        return 1.0 if ok and self._filter(actual) else 0.0


class _StringMatcher(Matcher):
    def __init__(self, additional_filter: Callable[[str], bool]) -> None:
        self._filter = additional_filter

    def match(self, actual: Any) -> float:
        return 1.0 if isinstance(actual, str) and self._filter(actual) else 0.0


class _CaseInsensitiveStringMatcher(Matcher):
    def __init__(self, expected: str) -> None:
        self._expected = expected.lower()

    def match(self, actual: Any) -> float:
        return 1.0 if isinstance(actual, str) and actual.lower() == self._expected else 0.0


class _BooleanMatcher(Matcher):
    def __init__(self, expected: bool | None) -> None:
        self._expected = expected

    def match(self, actual: Any) -> float:
        if not isinstance(actual, bool):
            return 0.0
        return 1.0 if self._expected is None or self._expected == actual else 0.0


class _NotMatcher(Matcher):
    def __init__(self, matcher: Matcher) -> None:
        self._matcher = matcher

    def match(self, actual: Any) -> float:
        return 0.0 if self._matcher.match(actual) == 1.0 else 1.0


class _CompositeMatcher(Matcher):
    def __init__(self, matchers: list[Matcher]) -> None:
        self._matchers = matchers

    def match(self, actual: Any) -> float:
        current = 0.0
        for matcher in self._matchers:
            score = matcher.match(actual)
            if score == 1.0:
                return 1.0
            current = max(current, score)
        return current


class _ArrayOrSingleMatcher(Matcher):
    def __init__(self, matcher: Matcher) -> None:
        self._matcher = matcher

    def match(self, actual: Any) -> float:
        if isinstance(actual, list):
            return 1.0 if len(actual) == 1 and self._matcher.match(actual[0]) == 1.0 else 0.0
        return self._matcher.match(actual)


class _ValueMatcher(Matcher):
    def __init__(self, expected: Any) -> None:
        self._expected = expected

    def match(self, actual: Any) -> float:
        expected = self._expected
        if expected == actual:
            return 1.0
        if expected is None:
            return 1.0 if actual is None or actual is MISSING else 0.0

        if isinstance(expected, list):
            if not isinstance(actual, list) or len(actual) > len(expected):
                return 0.0
            current = 1.0
            for i, item in enumerate(expected):
                actual_item = actual[i] if i < len(actual) else MISSING
                current = min(current, Matcher.value(item).match(actual_item))
                if current == 0.0:
                    return 0.0
            return current

        if isinstance(expected, dict):
            if not isinstance(actual, dict):
                return 0.0
            if len(actual) > len(expected):
                return 0.0
            current = 1.0
            for key, sub in expected.items():
                current = min(current, Matcher.value(sub).match(actual.get(key, MISSING)))
                if current == 0.0:
                    return 0.0
            return current

        return 0.0


Matcher.empty_object_or_undefined = _EmptyObjectOrUndefinedMatcher()
Matcher.any_value = _AnyValueMatcher()
Matcher.undefined = _UndefinedMatcher()
Matcher.null = _NullMatcher()
