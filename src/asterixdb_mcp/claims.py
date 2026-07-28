"""Claims extracted from note text: what value is asserted, under what label.

Two sides of the memory loop need the same primitive. The write side compares a
new note against the stored one to warn about a contradiction before it lands;
the read side compares a delivered note against the rows that just came back
from the cluster, to catch a note the data has since outgrown.

Everything here is text heuristics over short notes, so it is deliberately
conservative: a claim needs a label word next to the value, and the comparison
treats a value inside the stored range as agreement rather than conflict.
"""

from __future__ import annotations

import re
from collections import defaultdict

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z_]{2,}")

# How far from a number a word may sit and still be read as that number's label.
CONTEXT_CHARS = 30

# Clause boundaries: list separators, line breaks, and sentence-ending periods
# (a period inside "1.50" is not one).
SEGMENT_RE = re.compile(r"[,;\n]|\.\s")

# Structural words carry no claim; excluding them keeps the label-match honest.
STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "not",
        "are",
        "was",
        "this",
        "that",
        "from",
        "use",
        "using",
        "into",
        "have",
        "has",
    }
)

# Type vocabulary for the read-side type check. A note asserting one of these
# about a field is checkable against the value the cluster actually returned.
STRING_WORDS = frozenset({"string", "strings", "text", "textual", "varchar"})
NUMBER_WORDS = frozenset({"number", "numbers", "numeric", "integer", "int", "double", "float"})


def normalize_number(raw: str) -> str:
    """Normalize a NUMBER_RE match by value so "1" and "1.0" compare equal."""
    return str(float(raw))


def labels_near(text: str, start: int, end: int) -> set[str]:
    """Content words within CONTEXT_CHARS of the span, lowercased."""
    window = text[max(0, start - CONTEXT_CHARS) : end + CONTEXT_CHARS]
    return {w.lower() for w in WORD_RE.findall(window)} - STOPWORDS


def numeric_claims(text: str) -> dict[str, set[str]]:
    """Map each nearby word to the numeric values it appears with in ``text``."""
    claims: dict[str, set[str]] = defaultdict(set)
    for match in NUMBER_RE.finditer(text):
        value = normalize_number(match.group())
        for label in labels_near(text, match.start(), match.end()):
            claims[label].add(value)
    return claims


def segment_claims(text: str) -> dict[str, set[str]]:
    """Numeric claims scoped to the clause each number sits in.

    A breakdown reads as one claim per item: in "Operational (410), Low kV
    (17)" the plain window would hand 17 to "operational" as well, and a stored
    label spanning 17..410 then swallows every fresh value in between. Clause
    boundaries keep each label to the value actually written beside it.
    """
    claims: dict[str, set[str]] = defaultdict(set)
    for segment in SEGMENT_RE.split(text):
        for match in NUMBER_RE.finditer(segment):
            value = normalize_number(match.group())
            for label in labels_near(segment, match.start(), match.end()):
                claims[label].add(value)
    return claims


def values_conflict(new_values: set[str], old_values: set[str]) -> bool:
    """True when some new value is neither a stored value nor within their range."""
    if new_values & old_values:
        return False
    old_floats = [float(v) for v in old_values]
    low, high = min(old_floats), max(old_floats)
    return any(not (low <= float(v) <= high) for v in new_values)


def fmt_values(values: set[str]) -> str:
    return ", ".join(sorted(values, key=float))
