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
from ..staleness import SUSPECT_LABEL, check_rows, is_suspect, render_warning
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

# The same clauses with an unqualified name. A model that has issued `USE dv`
# writes `FROM substations s`, which _FROM_RE cannot see. The trailing lookahead
# drops anything followed by a dot - that is the qualified form, already matched
# above, or an alias.field path. It also bars a following identifier character,
# without which the group would simply backtrack to a shorter name to dodge the
# dot and yield `other_d` out of `other_dv.hospitals`. A leading `(` or `[`
# never matches, so subqueries and array literals stay out.
_BARE_FROM_RE = re.compile(
    r"\b(?:from|join|unnest)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?(?![A-Za-z0-9_`]|\s*\.)",
    re.IGNORECASE,
)

# The dataverse a statement selected for itself, which outranks any default the
# caller supplies.
_USE_RE = re.compile(r"\buse\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", re.IGNORECASE)


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
    """The delivery shape of note rows: subject, note text, evidence status.

    ``suspect`` carries the doubt raised when fresh data contradicted the note
    — set in this session, or inherited from a session that saw the same
    disagreement and left the marker on the row.
    """
    return [
        {
            "subject": row.get("subject"),
            "note": _note_text(row),
            "grounded": _grounded(row),
            "suspect": is_suspect(row),
        }
        for row in rows
    ]


async def fetch_memory_notes(
    client: CCClient, ccid: str, subjects: list[str]
) -> list[dict[str, Any]]:
    """Return current learned notes for the given concept subjects (best-effort)."""
    return display_notes(await fetch_note_rows(client, ccid, subjects))


async def deliver_notes(
    client: CCClient,
    ccid: str,
    subjects: list[str],
    *,
    result_rows: list[Any],
    whole_rows: bool = False,
    settings: Settings | None = None,
    recall: RecallState | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch the notes for these subjects, checked against the evidence at hand.

    Every tool that delivers notes goes through here, because a note is only
    checkable in the moment it rides out beside fresh rows. Recall is
    first-use-only, so whichever tool touches a dataset FIRST is the one and
    only chance to catch a contradiction for it — and that is usually a sample,
    not a query.

    Returns the notes in delivery shape and one line per contradiction found.
    """
    rows = await fetch_note_rows(client, ccid, subjects)
    if not rows:
        return [], []
    rows, contradictions = check_rows(rows, result_rows, whole_rows)
    if recall is not None:
        recall.remember(rows)
    # One write carries both the usage bump and any suspect marker, so the
    # reinforcement cannot overwrite a flag raised a moment earlier.
    if settings is not None and settings.memory_write_enabled:
        await reinforce_notes(client, ccid, rows)
    return display_notes(rows), contradictions


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

    The delivered ROWS are kept as well, because delivery and disproof rarely
    happen at the same moment. A model samples a dataset before it aggregates
    it, so the note arrives with the sample while the count that contradicts it
    arrives several turns later. Holding the rows lets that later result be
    checked without re-reading the store or repeating the notes.
    """

    def __init__(self) -> None:
        self._delivered: set[str] = set()
        self._rows: dict[str, list[dict[str, Any]]] = {}

    def fresh(self, subjects: list[str]) -> list[str]:
        """Subjects whose notes have not been delivered this session."""
        return [s for s in subjects if s not in self._delivered]

    def remember(self, rows: list[dict[str, Any]]) -> None:
        """Keep delivered note rows so later results can still be checked.

        Keyed by row id, or a note delivered twice would report the same
        contradiction twice.
        """
        for row in rows:
            subject = str(row.get("subject") or "")
            if not subject:
                continue
            kept = self._rows.setdefault(subject, [])
            if not any(k.get("id") == row.get("id") for k in kept):
                kept.append(row)

    def carried(self, subjects: list[str]) -> list[dict[str, Any]]:
        """Note rows already delivered for these subjects this session."""
        return [row for subject in subjects for row in self._rows.get(subject, [])]

    def mark(self, subjects: list[str]) -> None:
        """Record subjects whose notes were just delivered."""
        self._delivered.update(subjects)


def subjects_from_statement(statement: str, dataverse: str | None = None) -> list[str]:
    """Extract candidate concept subjects (dataverse.dataset) from a SQL++ statement.

    Unqualified collection names are resolved against the statement's own ``USE``
    clause, falling back to ``dataverse`` (the tool argument the caller ran the
    query under). With neither, a bare name is dropped rather than guessed at:
    the wrong dataverse would attach another dataset's notes to this statement.
    """
    subjects: list[str] = []
    for qualifier, dataset in _FROM_RE.findall(statement):
        _add_subject(subjects, qualifier, dataset)

    use_match = _USE_RE.search(statement)
    default = use_match.group(1) if use_match else dataverse
    if default is None:
        return subjects
    for dataset in _BARE_FROM_RE.findall(statement):
        _add_subject(subjects, default, dataset)
    return subjects


def _add_subject(subjects: list[str], dataverse: str, dataset: str) -> None:
    """Append ``dataverse.dataset`` unless it is Metadata or already present."""
    if dataverse == "Metadata":
        return
    subject = f"{dataverse}.{dataset}"
    if subject not in subjects:
        subjects.append(subject)


async def attach_statement_notes(
    client: CCClient,
    ccid: str,
    statement: str,
    result: ToolResult,
    recall: RecallState | None = None,
    first_use_only: bool = False,
    settings: Settings | None = None,
    dataverse: str | None = None,
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
    touched = subjects_from_statement(statement, dataverse)
    subjects = recall.fresh(touched) if (recall is not None and first_use_only) else touched
    rows = _result_rows(result)
    notes, contradictions = await deliver_notes(
        client, ccid, subjects, result_rows=rows, settings=settings, recall=recall
    )
    if recall is not None:
        recall.mark([str(n["subject"]) for n in notes if n.get("subject")])
        contradictions += await _recheck_carried(client, ccid, recall, touched, rows, settings)
    if not notes and not contradictions:
        return result
    structured = result.structured
    if structured is not None:
        structured = {**structured, "learnedNotes": notes} if notes else dict(structured)
        if contradictions:
            structured["staleNotes"] = contradictions
    delivered = (
        "\n\nLearned notes from memory about the referenced datasets:\n" + render_notes(notes)
        if notes
        else ""
    )
    return ToolResult(
        text=result.text + delivered + render_warning(contradictions),
        structured=structured,
        is_error=result.is_error,
    )


async def _recheck_carried(
    client: CCClient,
    ccid: str,
    recall: RecallState,
    subjects: list[str],
    result_rows: list[Any],
    settings: Settings | None,
) -> list[str]:
    """Check this result against notes delivered EARLIER in the session.

    Recall fires once per dataset, and it usually fires on the sample the model
    takes before it writes anything. The aggregate that can actually disprove a
    stored count lands turns later, with no notes attached — so without this the
    contradiction the whole check exists for is never seen. The notes are not
    repeated; only the disagreement is raised.
    """
    carried = recall.carried([s for s in subjects if s not in recall.fresh(subjects)])
    if not carried or not result_rows:
        return []
    flagged, contradictions = check_rows(carried, result_rows)
    if contradictions and settings is not None and settings.memory_write_enabled:
        await reinforce_notes(client, ccid, flagged)
    return contradictions


def _result_rows(result: ToolResult) -> list[Any]:
    """The fresh rows a result carries, or none when it carries no evidence.

    An error result has no rows to check against, and a tool whose payload is
    not a row list (schema, plans) is out of scope for the value checks.
    """
    if result.is_error or not isinstance(result.structured, dict):
        return []
    rows = result.structured.get("results")
    return rows if isinstance(rows, list) else []


def render_notes(notes: list[dict[str, Any]]) -> str:
    """One line per note; standalone notes carry their evidence status inline."""
    lines = []
    for n in notes:
        grounded = n.get("grounded")
        markers = [] if grounded is None else ["grounded" if grounded else "unverified"]
        if n.get("suspect"):
            markers.append(SUSPECT_LABEL)
        label = f" ({', '.join(markers)})" if markers else ""
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
