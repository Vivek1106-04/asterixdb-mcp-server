"""Stale-note detection: a delivered note checked against the rows it rode with.

Ambient recall attaches learned notes to the result of the query that touched
their dataset, which means the gateway holds both halves of a contradiction at
the same instant — the note it just injected, and fresh evidence from the
cluster. A standing "correct the store if it disagrees" instruction does not
fire; a pointed warning naming the disagreeing value does, and it is raised
exactly where the disagreement is visible.

Two contradictions are checkable without a language model:

- **value drift** — the note claims a number under a label the result also
  carries a number for, and the two never overlap. A stored count of 410
  against a fresh 290 for "Operational" is drift.
- **type change** — the note asserts a field is textual (or numeric) and the
  rows say otherwise. A note describing "K"/"M" suffixes on a field the cluster
  now returns as a double is the dangerous case: nothing errors, and applying
  the note's conversion rule inflates every figure a thousandfold.

Detection only ever flags. Notes are never rewritten or retired here — the
gateway must not invalidate knowledge on its own inference, and a false
positive that silences a good note is worse than a stale note that survives.
The flag is durable: the suspect marker rides on the note row, so a later
session sees the doubt even if this one ignores it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .claims import (
    NUMBER_WORDS,
    SEGMENT_RE,
    STOPWORDS,
    STRING_WORDS,
    WORD_RE,
    fmt_values,
    labels_near,
    normalize_number,
    segment_claims,
    values_conflict,
)

# Bounds on the work a single check may do; results are already egress-capped,
# these keep a wide row from turning recall into a text scan.
MAX_CHECKED_ROWS = 50
MAX_CONFLICTS_PER_NOTE = 3
MAX_REASON_LEN = 300
MIN_FIELD_NAME_LEN = 3
# A note only counts as field-describing once it names this many live fields.
MIN_FIELD_MENTIONS = 2

SUSPECT_LABEL = "possibly stale"

_STALE_HEADER = "STALE NOTE CHECK — the rows just returned disagree with the notes above:"
_STALE_FOOTER = (
    "Confirm against the data. If a note is wrong, correct it now with memory_write "
    "(replaces=<fragment of the stale note>, source_query=<the query that proved the "
    "new value>) — otherwise every later session inherits the same wrong note."
)


def field_types(rows: list[Any]) -> dict[str, str]:
    """Map each field to the single value type observed across the rows.

    Fields whose values are of mixed type carry no checkable assertion and are
    dropped, as are short names — a two-letter alias matches note prose by
    accident far more often than it matches a real claim.
    """
    observed: dict[str, set[str]] = defaultdict(set)
    for row in rows[:MAX_CHECKED_ROWS]:
        if not isinstance(row, dict):
            continue
        for field, value in row.items():
            kind = _value_kind(value)
            if kind is not None and len(str(field)) >= MIN_FIELD_NAME_LEN:
                observed[str(field)].add(kind)
    return {field: next(iter(kinds)) for field, kinds in observed.items() if len(kinds) == 1}


def _value_kind(value: Any) -> str | None:
    """The type word for values whose type a note can assert, else None."""
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return "number"
    return None


def result_claims(rows: list[Any]) -> dict[str, set[str]]:
    """Numeric claims the result rows make, labelled the way a note would be.

    A row's numbers are claimed under the row's own string values, so a grouped
    count like ``{"status": "Operational", "n": 290}`` claims 290 for
    "operational" — the same label a note reading "Operational (410)" claims
    410 for.

    Field NAMES are deliberately not labels. A note's "711 hospitals" and a
    result's per-county ``beds`` column both mention beds while measuring
    different things, and comparing them manufactures contradictions out of
    unlike quantities. Only a value paired with the category it belongs to is
    a like-for-like comparison.
    """
    claims: dict[str, set[str]] = defaultdict(set)
    for row in rows[:MAX_CHECKED_ROWS]:
        if not isinstance(row, dict):
            continue
        numbers = {
            normalize_number(str(value)) for value in row.values() if _value_kind(value) == "number"
        }
        if not numbers:
            continue
        for label in _row_value_labels(row):
            claims[label].update(numbers)
    return claims


def _row_value_labels(row: dict[str, Any]) -> set[str]:
    """Lowercased content words appearing as string VALUES in the row."""
    labels: set[str] = set()
    for value in row.values():
        if isinstance(value, str):
            labels |= labels_near(value, 0, len(value))
    return labels


def numeric_drift(note: str, rows: list[Any]) -> list[str]:
    """Descriptors of note numbers the fresh rows contradict under the same label."""
    stored = segment_claims(note)
    if not stored:
        return []
    fresh = result_claims(rows)
    measures = _measure_tokens(rows)
    out: list[str] = []
    for label, stored_values in sorted(stored.items()):
        fresh_values = fresh.get(label)
        if fresh_values is None or not values_conflict(fresh_values, stored_values):
            continue
        if not _measures_the_same_thing(note, label, measures):
            continue
        out.append(
            f"'{label}': note says {fmt_values(stored_values)}, "
            f"this result shows {fmt_values(fresh_values)}"
        )
        if len(out) == MAX_CONFLICTS_PER_NOTE:
            break
    return out


def _measure_tokens(rows: list[Any]) -> set[str]:
    """What the result measured: the words making up its field names."""
    tokens: set[str] = set()
    for field in field_types(rows):
        tokens |= {part for part in field.lower().split("_") if len(part) >= MIN_FIELD_NAME_LEN}
    return tokens


def _measures_the_same_thing(note: str, label: str, measures: set[str]) -> bool:
    """True when the note claims its number about the quantity the result reports.

    A category word alone is not enough: a note reading "Heat ... max 150
    deaths" and a row reading ``{"EVENT_TYPE": "Heat", "DAMAGE_PROPERTY": 0}``
    share the category and nothing else, and calling that a contradiction
    compares deaths against dollars. The claim must sit in a clause that also
    names one of the fields the result actually returned.
    """
    for segment in SEGMENT_RE.split(note):
        words = {w.lower() for w in WORD_RE.findall(segment)}
        if label in words and (words & measures) - {label}:
            return True
    return False


def type_drift(note: str, rows: list[Any]) -> list[str]:
    """Descriptors of fields the note types one way and the rows return another."""
    out: list[str] = []
    for field, actual in sorted(field_types(rows).items()):
        asserted = _asserted_type(note, field)
        if asserted is None or asserted == actual:
            continue
        out.append(f"'{field}': note describes it as {asserted}, this result returns {actual}")
        if len(out) == MAX_CONFLICTS_PER_NOTE:
            break
    return out


def _asserted_type(note: str, field: str) -> str | None:
    """The type the note asserts for ``field``, or None when it asserts neither.

    A mention flanked by BOTH vocabularies ("stored as a string, convert to a
    number") asserts nothing checkable and is skipped — that is the shape of a
    conversion instruction, not a type claim.
    """
    lowered = note.lower()
    # Word-bounded: a bare "lat" also lives inside "population", and the type
    # word beside THAT sentence has nothing to do with the field.
    pattern = rf"(?<![a-z0-9_]){re.escape(field.lower())}(?![a-z0-9_])"
    for match in re.finditer(pattern, lowered):
        nearby = labels_near(lowered, match.start(), match.end())
        says_string = bool(nearby & STRING_WORDS)
        says_number = bool(nearby & NUMBER_WORDS)
        if says_string != says_number:
            return "string" if says_string else "number"
    return None


def renamed_fields(note: str, rows: list[Any]) -> list[str]:
    """Descriptors of fields a note names that the rows carry under another name.

    Only meaningful against whole records: in a projected result a field is
    absent because the query did not ask for it, which says nothing about the
    schema. Even then a bare word is not a claim, so the check runs only on
    notes that demonstrably describe fields — ones already naming at least
    ``MIN_FIELD_MENTIONS`` fields the rows really carry — and reports a word
    only when a live field shares its stem, which is what a rename looks like.
    """
    fields = set(field_types(rows))
    words = {w.lower() for w in WORD_RE.findall(note)}
    live = {field.lower() for field in fields}
    if len(words & live) < MIN_FIELD_MENTIONS:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for word in sorted(words - live - STOPWORDS):
        renamed = _stem_match(word, fields)
        # One line per live field: "bed" and "beds" both point at bed_count,
        # and saying so twice adds nothing.
        if renamed is None or renamed in seen:
            continue
        seen.add(renamed)
        out.append(f"'{word}': the note names it, but the rows carry '{renamed}'")
        if len(out) == MAX_CONFLICTS_PER_NOTE:
            break
    return out


def _stem_match(word: str, fields: set[str]) -> str | None:
    """The live field that looks like ``word`` renamed by qualification.

    A rename qualifies the old name — ``beds`` becomes ``bed_count`` — so the
    live field must begin with the old stem AND a separator. Without the
    separator "counts" would claim ``county``, and "count" would claim
    ``bed_count``, neither of which names the same thing.
    """
    stem = word.rstrip("s")
    if len(stem) < MIN_FIELD_NAME_LEN:
        return None
    for field in sorted(fields):
        if field.lower().startswith(f"{stem}_"):
            return field
    return None


def note_conflicts(note: str, rows: list[Any], whole_rows: bool = False) -> list[str]:
    """Every checkable disagreement between one note and the fresh rows.

    ``whole_rows`` marks a payload of complete records (a sample), where a
    field's absence is evidence; a projected query result carries no such
    signal and is checked for values and types only.
    """
    if not note.strip() or not rows:
        return []
    if whole_rows:
        # Numeric drift is meaningless here. In a whole record every string
        # value sits beside every column, so a hospital's name would claim its
        # own latitude as a bed count. A sample proves shapes and types; only
        # an aggregate proves quantities.
        found = type_drift(note, rows) + renamed_fields(note, rows)
    else:
        found = numeric_drift(note, rows) + type_drift(note, rows)
    return found[:MAX_CONFLICTS_PER_NOTE]


def flag_suspect(row: dict[str, Any], conflicts: list[str]) -> dict[str, Any]:
    """A copy of the note row carrying the suspect marker for these conflicts.

    The marker is metadata on the same row id — the note's text is untouched,
    so nothing is lost if the reading turns out to be a false positive.
    """
    return {
        **row,
        "suspect_since": datetime.now(timezone.utc).isoformat(),
        "suspect_reason": "; ".join(conflicts)[:MAX_REASON_LEN],
    }


def is_suspect(row: dict[str, Any]) -> bool:
    return bool(row.get("suspect_since"))


def check_rows(
    rows: list[dict[str, Any]], result_rows: list[Any], whole_rows: bool = False
) -> tuple[list[dict[str, Any]], list[str]]:
    """Flag the note rows the fresh result contradicts.

    Returns the rows with suspect markers applied (unchanged where nothing
    disagrees) alongside one human-readable line per contradicted note.
    """
    flagged: list[dict[str, Any]] = []
    reported: list[str] = []
    for row in rows:
        conflicts = note_conflicts(str(_row_note_text(row)), result_rows, whole_rows)
        if not conflicts:
            flagged.append(row)
            continue
        flagged.append(flag_suspect(row, conflicts))
        reported.append(f"- [{row.get('subject')}] " + "; ".join(conflicts))
    return flagged, reported


def _row_note_text(row: dict[str, Any]) -> str:
    """The learned text of a note row, whichever layer holds it."""
    return str(row.get("overlay") or row.get("text") or "")


def render_warning(reported: list[str]) -> str:
    """The conditional trigger appended after the delivered notes."""
    if not reported:
        return ""
    return "\n\n" + "\n".join([_STALE_HEADER, *reported, _STALE_FOOTER])
