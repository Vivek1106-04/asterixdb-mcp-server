"""Server-side auto-recall of learned memory notes.

Read tools attach the current learned notes for the concepts they touch, so
recall never depends on the client model choosing to call memory_search: the
schema for a dataset arrives WITH the caveats prior sessions recorded about
it, and a failed query carries the notes for the datasets it referenced.

Lookups are strictly best-effort. A missing or unreachable memory store must
never break the primary tool, so every failure path degrades to "no notes".
"""

from __future__ import annotations

import re
from typing import Any

from ..cc_client import CCClient
from ..errors import GatewayError
from . import ToolResult
from .memory_search import _IDENTIFIER_RE, MEMORY_DATASET

MAX_NOTE_SUBJECTS = 4
MAX_NOTE_LEN = 500

# Appended to schema/sample results when a dataset has no learned notes yet, so
# the write-side of the memory loop is prompted exactly where exploration happens.
NO_NOTES_HINT = (
    "No learned notes exist for {subject} yet — persist durable findings "
    "(field formats, gotchas, proven query patterns) with memory_write."
)

_NOTES_QUERY = (
    f"SELECT VALUE m FROM {MEMORY_DATASET} m "
    "WHERE m.subject IN $subjects AND m.valid_to IS UNKNOWN;"
)

# Qualified collection references in FROM/JOIN/UNNEST clauses; deliberately not
# a general dotted-path match so alias.field accesses stay out.
_FROM_RE = re.compile(
    r"\b(?:from|join|unnest)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\.\s*`?([A-Za-z_][A-Za-z0-9_]*)`?",
    re.IGNORECASE,
)


async def fetch_memory_notes(
    client: CCClient, ccid: str, subjects: list[str]
) -> list[dict[str, Any]]:
    """Return current learned notes for the given concept subjects (best-effort)."""
    wanted: list[str] = []
    for subject in subjects:
        if subject and _IDENTIFIER_RE.match(subject) and subject not in wanted:
            wanted.append(subject)
        if len(wanted) == MAX_NOTE_SUBJECTS:
            break
    if not wanted:
        return []
    try:
        envelope = await client.execute(
            _NOTES_QUERY, client_context_id=ccid, statement_parameters={"subjects": wanted}
        )
    except GatewayError:
        return []
    notes: list[dict[str, Any]] = []
    for row in envelope.get("results", []):
        if not isinstance(row, dict):
            continue
        text = _note_text(row)
        if text:
            notes.append({"subject": row.get("subject"), "note": text, "grounded": _grounded(row)})
    return notes


class RecallState:
    """Session-scoped memory of which subjects already had notes delivered.

    Ambient recall attaches learned notes to the first successful query that
    touches a dataset in a session; repeating them on every query would only
    burn the client's context window.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def claim(self, subjects: list[str], *, first_use_only: bool) -> list[str]:
        """Mark subjects as delivered; with first_use_only, return only new ones."""
        fresh = [s for s in subjects if s not in self._seen]
        self._seen.update(subjects)
        return fresh if first_use_only else subjects


def subjects_from_statement(statement: str) -> list[str]:
    """Extract candidate concept subjects (dataverse.dataset) from a SQL++ statement."""
    subjects: list[str] = []
    for dataverse, dataset in _FROM_RE.findall(statement):
        if dataverse == "Metadata":
            continue
        subject = f"{dataverse}.{dataset}"
        if subject not in subjects:
            subjects.append(subject)
    return subjects


async def attach_statement_notes(
    client: CCClient,
    ccid: str,
    statement: str,
    result: ToolResult,
    recall: RecallState | None = None,
    first_use_only: bool = False,
) -> ToolResult:
    """Append learned notes for the statement's datasets to a tool result.

    With a RecallState and first_use_only, notes are attached only for datasets
    not yet covered this session; either way delivered subjects are recorded so
    later attachments do not repeat them.
    """
    subjects = subjects_from_statement(statement)
    if recall is not None:
        subjects = recall.claim(subjects, first_use_only=first_use_only)
    notes = await fetch_memory_notes(client, ccid, subjects)
    if not notes:
        return result
    structured = result.structured
    if structured is not None:
        structured = {**structured, "learnedNotes": notes}
    return ToolResult(
        text=result.text
        + "\n\nLearned notes from memory about the referenced datasets:\n"
        + render_notes(notes),
        structured=structured,
        is_error=result.is_error,
    )


def render_notes(notes: list[dict[str, Any]]) -> str:
    """One line per note; standalone notes carry their evidence status inline."""
    lines = []
    for n in notes:
        grounded = n.get("grounded")
        label = "" if grounded is None else (" (grounded)" if grounded else " (unverified)")
        lines.append(f"- [{n['subject']}]{label} {n['note']}")
    return "\n".join(lines)


def _note_text(row: dict[str, Any]) -> str:
    # Walk-owned concepts carry learned knowledge only in their overlay; their
    # core restates what the schema tools already return, so it is skipped.
    if _is_walk_owned(row):
        text = str(row.get("overlay") or "")
    else:
        text = str(row.get("text") or "")
    return text.strip()[:MAX_NOTE_LEN]


def _grounded(row: dict[str, Any]) -> bool | None:
    """Evidence status: None for overlay rows (their lines carry their own
    markers), else whether the note has a source_query backing it."""
    if _is_walk_owned(row):
        return None
    return bool(row.get("source_query"))


def _is_walk_owned(row: dict[str, Any]) -> bool:
    return str(row.get("type", "")).startswith("AsterixDB ")
