"""Server-side auto-recall of learned memory notes.

Read tools attach the current learned notes for the concepts they touch, so
recall never depends on the client model choosing to call memory_search: the
schema for a dataset arrives WITH the caveats prior sessions recorded about
it, and a failed query carries the notes for the datasets it referenced.

Lookups are strictly best-effort. A missing or unreachable memory store must
never break the primary tool, so every failure path degrades to "no notes".
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..errors import GatewayError
from . import ToolResult
from .memory_search import _IDENTIFIER_RE, MEMORY_DATASET

MAX_NOTE_SUBJECTS = 4
MAX_NOTE_LEN = 500

# Recall ranking: with many notes on the touched datasets, only the strongest
# attach — evidence-backed knowledge outranks assertions, and notes that keep
# proving useful (recalled often, recently) outrank ones nothing ever reads.
MAX_ATTACHED_NOTES = 6
VERIFIED_WEIGHT = 2.0
FRESHNESS_HALF_LIFE_DAYS = 30.0
_UNKNOWN_AGE_DAYS = FRESHNESS_HALF_LIFE_DAYS * 12  # undated notes rank as old

_REINFORCE_UPSERT = f"UPSERT INTO {MEMORY_DATASET} ([$row]);"

# Appended to schema/sample results when a dataset has no learned notes yet, so
# the write-side of the memory loop is prompted exactly where exploration happens.
NO_NOTES_HINT = (
    "No learned notes exist for {subject} yet — persist durable findings "
    "(field formats, gotchas, proven query patterns) with memory_write."
)

# Appended after delivered notes so datasets that already have SOME knowledge
# still prompt for the findings the notes do not cover yet.
HAVE_NOTES_HINT = (
    "If you discover a durable finding the notes above do not cover, persist it "
    "with memory_write and include the proving query as source_query."
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


async def fetch_note_rows(client: CCClient, ccid: str, subjects: list[str]) -> list[dict[str, Any]]:
    """Current note-bearing memory rows for the given subjects, ranked by score.

    Direct subject hits are followed one hop across their ``links`` so a
    dataset's query brings in the learned notes on its related concepts (its
    indexes and datatype) too — only linked concepts that actually carry a
    learned note contribute. Best-effort: any store failure degrades to no
    rows. The combined pool is capped at MAX_ATTACHED_NOTES, strongest first
    (see ``note_score``).
    """
    wanted: list[str] = []
    for subject in subjects:
        if subject and _IDENTIFIER_RE.match(subject) and subject not in wanted:
            wanted.append(subject)
        if len(wanted) == MAX_NOTE_SUBJECTS:
            break
    if not wanted:
        return []
    direct = await _query_note_rows(client, ccid, wanted)
    linked_subjects = _link_subjects(direct, seen=set(wanted))
    pool = direct
    if linked_subjects:
        pool = _dedupe_rows(direct + await _query_note_rows(client, ccid, linked_subjects))
    now = datetime.now(timezone.utc)
    ranked = sorted(pool, key=lambda row: note_score(row, now), reverse=True)
    return ranked[:MAX_ATTACHED_NOTES]


async def _query_note_rows(
    client: CCClient, ccid: str, subjects: list[str]
) -> list[dict[str, Any]]:
    """Fetch current note-bearing rows for exact subjects (best-effort)."""
    try:
        envelope = await client.execute(
            _NOTES_QUERY, client_context_id=ccid, statement_parameters={"subjects": subjects}
        )
    except GatewayError:
        return []
    return [row for row in envelope.get("results", []) if isinstance(row, dict) and _note_text(row)]


def _link_subjects(rows: list[dict[str, Any]], seen: set[str]) -> list[str]:
    """Valid, unseen link targets from a row set, capped for the one-hop follow."""
    out: list[str] = []
    for row in rows:
        for link in row.get("links") or []:
            target = str(link)
            if target in seen or target in out or not _IDENTIFIER_RE.match(target):
                continue
            out.append(target)
            if len(out) == MAX_NOTE_SUBJECTS:
                return out
    return out


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop rows sharing an id, keeping first occurrence (direct hit wins)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def display_notes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The delivery shape of note rows: subject, note text, evidence status."""
    return [
        {"subject": row.get("subject"), "note": _note_text(row), "grounded": _grounded(row)}
        for row in rows
    ]


async def fetch_memory_notes(
    client: CCClient, ccid: str, subjects: list[str]
) -> list[dict[str, Any]]:
    """Return current learned notes for the given concept subjects (best-effort)."""
    return display_notes(await fetch_note_rows(client, ccid, subjects))


def note_score(row: dict[str, Any], now: datetime) -> float:
    """Recall-utility score: evidence + usage + freshness.

    Walk-owned concepts and grounded notes carry the verified weight; usage
    grows logarithmically with recall_count; freshness decays with the days
    since the note was last recalled (or written).
    """
    verified = VERIFIED_WEIGHT if _is_walk_owned(row) or row.get("source_query") else 0.0
    usage = math.log1p(float(row.get("recall_count") or 0))
    freshness = 1.0 / (1.0 + _age_days(row, now) / FRESHNESS_HALF_LIFE_DAYS)
    return verified + usage + freshness


def _age_days(row: dict[str, Any], now: datetime) -> float:
    stamp = row.get("last_recalled_at") or row.get("valid_from")
    try:
        then = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return _UNKNOWN_AGE_DAYS
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max((now - then).total_seconds() / 86400.0, 0.0)


async def reinforce_notes(client: CCClient, ccid: str, rows: list[dict[str, Any]]) -> None:
    """Bump usage counters on rows whose notes were just delivered (best-effort).

    The bump rewrites the SAME row id with recall_count/last_recalled_at
    updated — usage is metadata, not new knowledge, so it must not create
    bi-temporal churn. Concurrent bumps are last-writer-wins; the counters are
    approximate by design.
    """
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        bumped = {
            **row,
            "recall_count": int(row.get("recall_count") or 0) + 1,
            "last_recalled_at": now,
        }
        try:
            await client.execute_memory_write(
                _REINFORCE_UPSERT, client_context_id=ccid, statement_parameters={"row": bumped}
            )
        except GatewayError:
            return


class RecallState:
    """Session-scoped memory of which subjects already had notes delivered.

    Ambient recall attaches learned notes to the first successful query that
    touches a dataset in a session; repeating them on every query would only
    burn the client's context window. Only subjects whose notes were actually
    DELIVERED are marked: a dataset with no notes yet stays fresh, so a note
    written later in the session still surfaces on its next query.
    """

    def __init__(self) -> None:
        self._delivered: set[str] = set()

    def fresh(self, subjects: list[str]) -> list[str]:
        """Subjects whose notes have not been delivered this session."""
        return [s for s in subjects if s not in self._delivered]

    def mark(self, subjects: list[str]) -> None:
        """Record subjects whose notes were just delivered."""
        self._delivered.update(subjects)


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
    settings: Settings | None = None,
) -> ToolResult:
    """Append learned notes for the statement's datasets to a tool result.

    With a RecallState and first_use_only, notes are attached only for datasets
    not yet covered this session; either way the subjects actually delivered
    are recorded so later attachments do not repeat them. When ``settings``
    allows memory writes, delivery also reinforces the delivered rows' usage
    counters, feeding the recall-utility score and the decay pass. With the
    memory surface disabled the result passes through untouched.
    """
    if settings is not None and not settings.memory_enabled:
        return result
    subjects = subjects_from_statement(statement)
    if recall is not None and first_use_only:
        subjects = recall.fresh(subjects)
    rows = await fetch_note_rows(client, ccid, subjects)
    notes = display_notes(rows)
    if not notes:
        return result
    if recall is not None:
        recall.mark([str(n["subject"]) for n in notes if n.get("subject")])
    if settings is not None and settings.memory_write_enabled:
        await reinforce_notes(client, ccid, rows)
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
    source = row.get("overlay") if _is_walk_owned(row) else row.get("text")
    return str(source or "").strip()[:MAX_NOTE_LEN]


def _grounded(row: dict[str, Any]) -> bool | None:
    """Evidence status: None for overlay rows (their lines carry their own
    markers), else whether the note has a source_query backing it."""
    if _is_walk_owned(row):
        return None
    return bool(row.get("source_query"))


def _is_walk_owned(row: dict[str, Any]) -> bool:
    return str(row.get("type", "")).startswith("AsterixDB ")
