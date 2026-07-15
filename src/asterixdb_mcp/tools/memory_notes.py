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
            notes.append({"subject": row.get("subject"), "note": text})
    return notes


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
    client: CCClient, ccid: str, statement: str, result: ToolResult
) -> ToolResult:
    """Append learned notes for the statement's datasets to a tool result's text."""
    notes = await fetch_memory_notes(client, ccid, subjects_from_statement(statement))
    if not notes:
        return result
    return ToolResult(
        text=result.text
        + "\n\nLearned notes from memory about the referenced datasets:\n"
        + render_notes(notes),
        structured=result.structured,
        is_error=result.is_error,
    )


def render_notes(notes: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{n['subject']}] {n['note']}" for n in notes)


def _note_text(row: dict[str, Any]) -> str:
    # Walk-owned concepts carry learned knowledge only in their overlay; their
    # core restates what the schema tools already return, so it is skipped.
    if str(row.get("type", "")).startswith("AsterixDB "):
        text = str(row.get("overlay") or "")
    else:
        text = str(row.get("text") or "")
    return text.strip()[:MAX_NOTE_LEN]
