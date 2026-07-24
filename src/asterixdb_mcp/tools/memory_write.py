"""memory_write: agent-curated writes into the OKF memory store.

The write side of the agentic-memory loop: an agent that learned something
worth keeping (a caveat, a proven query, business meaning) persists it as an
OKF concept row in ``AgentMemory.Memory``. Reconciliation is bi-temporal and
layer-aware, mirroring the refresh pipeline:

- unknown subject       -> a new standalone ``Note`` concept is inserted,
- catalog concept       -> the note lands in the concept's learned *overlay*
  (the walk-owned core is never touched); the current row is superseded and
  re-inserted with the annotation appended,
- existing note         -> replaced bi-temporally (superseded, re-inserted),
- text already present  -> no-op, UNLESS the rewrite adds a ``source_query`` an
  unverified copy lacked — that upgrade re-grounds the note instead of deduping
  the evidence away.

Corrections: overlay annotations are otherwise append-only, so a contradicted
note would sit next to its correction forever. Passing ``replaces`` retires
every overlay block containing that fragment (case-insensitive) before the new
note is appended; the retired lines survive in the superseded row.

Nothing is ever deleted; superseded rows remain the concept's history.

Defense-in-Depth:
- Layer 1: the schema says writes are gated by ``memory_write_enabled`` and
  scoped to the memory store.
- Layer 2: subject/link identifiers are validated against a strict charset,
  the note body is length-capped, and row values are bound as statement
  parameters — no client text is spliced into SQL++. The CC client re-checks
  the gate and the statement shape before anything leaves the gateway.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError
from ..inventory import dataset_names, dataverse_names, fetch_dataset_rows
from . import ToolResult
from .memory_notes import reinforce_notes
from .memory_search import _IDENTIFIER_RE, MEMORY_DATASET

MAX_TEXT_LEN = 4000
MAX_LINKS = 16
NOTE_TYPE = "Note"
KIND = "semantic"

# Numeric-conflict detection: a cheap, deterministic nudge. When a new note
# carries a number under the same nearby word as the stored note but the values
# never overlap, the write still lands (append-only truth), and the response
# flags the possible contradiction so the model resolves it with `replaces`.
MAX_REPORTED_CONFLICTS = 3
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z_]{2,}")
_CONFLICT_CONTEXT_CHARS = 30
# Near-duplicate rejection: a paraphrase whose content tokens are almost all
# already present in one stored block adds noise, not knowledge. Containment
# (not Jaccard) so a short restatement of a long stored note still matches.
NEAR_DUP_THRESHOLD = 0.8
DUP_PREVIEW_LEN = 160
# Structural words carry no claim; excluding them keeps the label-match honest.
_CONFLICT_STOPWORDS = frozenset(
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

_CURRENT_QUERY = (
    f"SELECT VALUE m FROM {MEMORY_DATASET} m WHERE m.subject = $subject AND m.valid_to IS UNKNOWN;"
)
# the engine binds $row as an object; the array constructor around it is
# statement text because a bound array is not accepted as an INSERT body
_UPSERT = f"UPSERT INTO {MEMORY_DATASET} ([$row]);"
_INSERT = f"INSERT INTO {MEMORY_DATASET} ([$row]);"


async def run_memory_write(
    client: CCClient,
    settings: Settings,
    *,
    subject: str,
    text: str,
    links: list[str] | None = None,
    tags: list[str] | None = None,
    source_query: str | None = None,
    replaces: str | None = None,
    author: str | None = None,
) -> ToolResult:
    """Persist one agent-curated note, reconciled bi-temporally by subject.

    ``author`` is provenance ("client-name/version"), stamped on the written
    row so the store records which connected client produced each note.
    """
    if not settings.memory_write_enabled:
        return ToolResult.error(
            GatewayError(
                ErrorType.FORBIDDEN,
                "Memory writes are disabled on this gateway. Ask the operator to set "
                "ASTERIXDB_MCP_MEMORY_WRITE_ENABLED=true; every other statement stays "
                "read-only either way.",
            )
        )
    note = text.strip()
    if not note:
        return ToolResult.error(
            GatewayError(ErrorType.INVALID_PARAMETER, "Provide a non-empty note text.")
        )
    if len(note) > MAX_TEXT_LEN:
        return ToolResult.error(
            GatewayError(
                ErrorType.INVALID_PARAMETER,
                f"Note text is too long (max {MAX_TEXT_LEN} characters); distill it first.",
            )
        )
    for label, values in (("subject", [subject]), ("links", links or [])):
        for value in values:
            if not _IDENTIFIER_RE.match(value):
                return ToolResult.error(
                    GatewayError(
                        ErrorType.INVALID_PARAMETER,
                        f"Invalid {label} {value!r}: expected a concept identifier "
                        "(letters, digits, '_', '.', '/', '@', '-').",
                    )
                )
    if links and len(links) > MAX_LINKS:
        return ToolResult.error(
            GatewayError(ErrorType.INVALID_PARAMETER, f"Too many links (max {MAX_LINKS}).")
        )
    if replaces is not None:
        replaces = replaces.strip()
        if not replaces:
            return ToolResult.error(
                GatewayError(
                    ErrorType.INVALID_PARAMETER,
                    "Provide a non-empty replaces fragment, or omit it.",
                )
            )

    ccid = make_client_context_id(settings.agent_session_id, "memory_write")
    # Agents often address a dataset by its bare name ('flood_zones'); the walk
    # owns the qualified concept ('real_estate.flood_zones'). Writing under the
    # bare name splits knowledge across two subjects, so ambient recall and the
    # near-duplicate check both miss it — canonicalize first.
    original_subject = subject
    subject = await _canonicalize_subject(client, ccid, subject)
    try:
        envelope = await client.execute(
            _CURRENT_QUERY, client_context_id=ccid, statement_parameters={"subject": subject}
        )
        rows = [row for row in envelope.get("results", []) if isinstance(row, dict)]
        existing = rows[0] if rows else None
        now = datetime.now(timezone.utc).isoformat()

        # Compare against the stored note BEFORE it is superseded; a correction
        # via `replaces` already retires the stale line, so only flag when the
        # writer did not signal one.
        stored_text = _existing_note_text(existing)
        conflicts = [] if replaces else numeric_conflicts(note, stored_text)

        # Near-duplicate paraphrases are rejected before any write: agents that
        # just read a fact in an ambient note tend to re-record it in their own
        # words, bloating the store. Exact repeats fall through to _reconcile's
        # "unchanged"/"regrounded" handling; corrections (replaces) always land,
        # and evidence lands when it upgrades an UNVERIFIED stored block — a
        # second proof of an already-grounded block adds nothing, so it is
        # rejected too, but counted: the rejection reinforces the existing row's
        # usage so the decay pass sees a repeatedly-confirmed fact as alive.
        if replaces is None and note not in stored_text:
            # A duplicate implies stored text, so `existing` is a row here.
            duplicate = near_duplicate_block(note, stored_text)
            if duplicate is not None and (
                source_query is None or _block_grounded(duplicate, existing or {})
            ):
                await reinforce_notes(client, ccid, [existing or {}])
                return _duplicate_result(
                    subject,
                    duplicate,
                    original_subject,
                    grounded=_block_grounded(duplicate, existing or {}),
                )

        action, row, retired = _reconcile(
            existing, subject, note, now, links, tags, source_query, replaces
        )
        if author and action != "unchanged":
            row = {**row, "author": author}
        if action == "unchanged":
            return ToolResult(
                text=f"Memory for '{subject}' already contains this note; nothing written.",
                structured={
                    "status": "success",
                    "subject": subject,
                    "action": action,
                    "id": None,
                    "retired": 0,
                    **_canonicalized_field(subject, original_subject),
                },
            )
        if existing is not None:
            await client.execute_memory_write(
                _UPSERT,
                client_context_id=ccid,
                statement_parameters={"row": {**existing, "valid_to": now}},
            )
        await client.execute_memory_write(
            _INSERT, client_context_id=ccid, statement_parameters={"row": row}
        )
    except GatewayError as err:
        return ToolResult.error(err)
    retired_text = f", {retired} outdated line(s) retired" if retired else ""
    structured: dict[str, Any] = {
        "status": "success",
        "subject": subject,
        "action": action,
        "id": row["id"],
        "retired": retired,
        "verified": bool(source_query),
        **_canonicalized_field(subject, original_subject),
    }
    if conflicts:
        structured["conflicts"] = conflicts
    return ToolResult(
        text=f"Memory {action}: '{subject}' ({row['id']}){retired_text}."
        + _verification_guidance(source_query, retired)
        + _conflict_warning(conflicts),
        structured=structured,
    )


def _existing_note_text(existing: dict[str, Any] | None) -> str:
    """The stored note text to compare a new note against (overlay for walk-owned)."""
    if existing is None:
        return ""
    source = existing.get("overlay") if _is_walk_owned(existing) else existing.get("text")
    return str(source or "")


def _content_tokens(text: str) -> set[str]:
    """Claim-bearing tokens: content words plus value-normalized numbers.

    Numbers are included so a paraphrase carrying DIFFERENT figures is new
    information, never a near-duplicate of the stored note.
    """
    words = {w.lower() for w in _WORD_RE.findall(text)} - _CONFLICT_STOPWORDS
    numbers = {_normalize_number(n) for n in _NUMBER_RE.findall(text)}
    return words | numbers


def near_duplicate_block(note: str, existing_text: str) -> str | None:
    """The stored block the note paraphrases, or None when it adds new content.

    A block is a near-duplicate when at least ``NEAR_DUP_THRESHOLD`` of the new
    note's content tokens already appear in it. Numbers are decisive: a note
    carrying ANY figure the block lacks is new information, never a duplicate,
    no matter how much wording it shares. Empty token sets never match — there
    is no claim to compare.
    """
    new_tokens = _content_tokens(note)
    if not new_tokens:
        return None
    new_numbers = {_normalize_number(n) for n in _NUMBER_RE.findall(note)}
    for block in (b.strip() for b in existing_text.split("\n\n")):
        if not block:
            continue
        block_tokens = _content_tokens(block)
        if new_numbers - block_tokens:
            continue
        contained = len(new_tokens & block_tokens) / len(new_tokens)
        if contained >= NEAR_DUP_THRESHOLD:
            return block
    return None


async def _canonicalize_subject(client: CCClient, ccid: str, subject: str) -> str:
    """Resolve a bare dataset name to its 'Dataverse.Dataset' concept identity.

    Only bare names are touched (no '.' or '/'), and only on an exact,
    unambiguous match against the catalog inventory; dataverse names, unknown
    names, and ambiguous names (same dataset in two dataverses) pass through
    unchanged. Best-effort: an unreachable catalog never blocks the write.
    """
    if "." in subject or "/" in subject:
        return subject
    try:
        rows = await fetch_dataset_rows(client, ccid=ccid)
    except GatewayError:
        return subject
    dataverses = dataverse_names(rows)
    if subject in dataverses:
        return subject
    owners = [dv for dv in dataverses if subject in dataset_names(rows, dv)]
    if len(owners) == 1:
        return f"{owners[0]}.{subject}"
    return subject


def _block_grounded(block: str, existing: dict[str, Any]) -> bool:
    """Whether the matched stored block already carries query evidence.

    Overlay blocks mark unevidenced claims inline with '(unverified)'; a block
    without the marker is grounded. Standalone notes ground the whole row via
    its source_query field.
    """
    if _is_walk_owned(existing):
        return "(unverified)" not in block
    return bool(existing.get("source_query"))


def _canonicalized_field(subject: str, original: str) -> dict[str, str]:
    """The structured-payload field recording a canonicalized subject, if any."""
    return {} if subject == original else {"canonicalizedFrom": original}


def _duplicate_result(
    subject: str, duplicate: str, original_subject: str, grounded: bool = False
) -> ToolResult:
    preview = duplicate if len(duplicate) <= DUP_PREVIEW_LEN else duplicate[:DUP_PREVIEW_LEN] + "…"
    if grounded:
        guidance = (
            "The stored note is already grounded; your confirmation reinforced it — "
            "nothing further to do. If your note adds a genuinely new fact, rephrase "
            "it to state only the new part; if it corrects the stored note, rewrite "
            "with replaces=<fragment of the stored note>."
        )
    else:
        guidance = (
            "Nothing written. If your note adds a genuinely new fact, rephrase it to "
            "state only the new part; if it corrects the stored note, rewrite with "
            "replaces=<fragment of the stored note>; if a query proved it, include "
            "source_query to ground the stored claim."
        )
    return ToolResult(
        text=f"Memory for '{subject}' already covers this — stored note: \"{preview}\". "
        + guidance,
        structured={
            "status": "success",
            "subject": subject,
            "action": "duplicate",
            "id": None,
            "retired": 0,
            "duplicateOf": preview,
            "duplicateGrounded": grounded,
            **_canonicalized_field(subject, original_subject),
        },
    )


def _numeric_claims(text: str) -> dict[str, set[str]]:
    """Map each nearby word to the numeric values it appears with in ``text``.

    Numbers are normalized by float value so "1" and "1.0" agree; a word within
    ``_CONFLICT_CONTEXT_CHARS`` of a number is that number's label.
    """
    claims: dict[str, set[str]] = defaultdict(set)
    for match in _NUMBER_RE.finditer(text):
        value = _normalize_number(match.group())
        start = max(0, match.start() - _CONFLICT_CONTEXT_CHARS)
        window = text[start : match.end() + _CONFLICT_CONTEXT_CHARS]
        for word in _WORD_RE.findall(window):
            lowered = word.lower()
            if lowered not in _CONFLICT_STOPWORDS:
                claims[lowered].add(value)
    return claims


def _normalize_number(raw: str) -> str:
    # raw is always a _NUMBER_RE match, so float() cannot fail here; normalizing
    # by value lets "1" and "1.0" compare equal.
    return str(float(raw))


def numeric_conflicts(new_note: str, existing_text: str) -> list[str]:
    """Human-readable descriptors of numeric claims that contradict the store.

    A conflict is a word carrying numbers in BOTH notes where a new value
    neither matches a stored value nor falls within the stored value range.
    The range check is what keeps a point inside a stored interval (a 2.5 under
    a stored "1.0 to 5.0") from reading as a contradiction. Heuristic and
    deliberately conservative — it warns, never blocks.
    """
    if not existing_text.strip():
        return []
    new_claims = _numeric_claims(new_note)
    old_claims = _numeric_claims(existing_text)
    conflicts: list[str] = []
    for label, new_values in sorted(new_claims.items()):
        old_values = old_claims.get(label)
        if old_values is None or not _values_conflict(new_values, old_values):
            continue
        conflicts.append(
            f"'{label}': stored {_fmt_values(old_values)} vs new {_fmt_values(new_values)}"
        )
        if len(conflicts) == MAX_REPORTED_CONFLICTS:
            break
    return conflicts


def _values_conflict(new_values: set[str], old_values: set[str]) -> bool:
    """True when some new value is neither a stored value nor within their range."""
    if new_values & old_values:
        return False
    old_floats = [float(v) for v in old_values]
    low, high = min(old_floats), max(old_floats)
    return any(not (low <= float(v) <= high) for v in new_values)


def _fmt_values(values: set[str]) -> str:
    return ", ".join(sorted(values, key=float))


def _conflict_warning(conflicts: list[str]) -> str:
    if not conflicts:
        return ""
    joined = "; ".join(conflicts)
    return (
        f" Possible conflict with an existing note on this subject ({joined}). "
        "If the new value is correct, rewrite with replaces=<fragment of the "
        "stale note> so the contradiction does not sit in the store."
    )


def _verification_guidance(source_query: str | None, retired: int) -> str:
    """Nudge the writer to ground unverified claims — hardest when a correction
    just replaced prior knowledge on nothing but assertion."""
    if source_query:
        return ""
    if retired:
        return (
            " The replacement is UNVERIFIED and just displaced prior knowledge: "
            "confirm it with a query against the data, then write it again with "
            "source_query so revalidation keeps it correct."
        )
    return (
        " Note stored unverified — when a query can prove this fact, include it "
        "as source_query so the note stays grounded."
    )


def _reconcile(
    existing: dict[str, Any] | None,
    subject: str,
    note: str,
    now: str,
    links: list[str] | None,
    tags: list[str] | None,
    source_query: str | None,
    replaces: str | None = None,
) -> tuple[str, dict[str, Any], int]:
    """Decide the write action and build the replacement row (pure).

    Returns (action, row, retired) where retired counts the overlay blocks
    removed because they contained the ``replaces`` fragment.
    """
    optional = {
        key: value
        for key, value in (("links", links), ("tags", tags), ("source_query", source_query))
        if value
    }
    if existing is None:
        return (
            "created",
            {
                "id": f"{subject}@{now}",
                "subject": subject,
                "type": NOTE_TYPE,
                "kind": KIND,
                "text": note,
                "valid_from": now,
                "trust": 1.0,
                "last_used": now,
                **optional,
            },
            0,
        )
    if _is_walk_owned(existing):
        core = str(existing.get("core") or existing.get("text", ""))
        overlay = str(existing.get("overlay") or "")
        kept, retired = _retire_overlay_blocks(overlay, replaces)
        action = "annotated"
        unverified_form = f"(unverified) {note}"
        if source_query and any(unverified_form in block for block in kept):
            # Re-grounding: the same note arriving WITH evidence upgrades the
            # stored unverified line instead of deduping the evidence away.
            kept = [block.replace(unverified_form, note) for block in kept]
            action = "regrounded"
        elif retired == 0 and note in overlay:
            return "unchanged", {}, 0
        if not any(note in block for block in kept):
            # Overlay lines carry their evidence status inline so auto-recall
            # can show readers which claims a query actually proved.
            kept = [*kept, note if source_query else f"(unverified) {note}"]
        new_overlay = "\n\n".join(kept) + "\n"
        return (
            action,
            {
                **existing,
                "id": f"{subject}@{now}",
                "valid_from": now,
                "core": core,
                "overlay": new_overlay,
                "text": core.rstrip("\n") + "\n\n" + new_overlay,
                "last_used": now,
            },
            retired,
        )
    if str(existing.get("text", "")).strip() == note:
        if source_query and not existing.get("source_query"):
            # Same standalone note now backed by evidence: supersede with the
            # source_query attached rather than discarding the grounding.
            return (
                "regrounded",
                {
                    **existing,
                    "id": f"{subject}@{now}",
                    "valid_from": now,
                    "trust": 1.0,
                    "last_used": now,
                    **optional,
                },
                0,
            )
        return "unchanged", {}, 0
    return (
        "superseded",
        {
            **existing,
            "id": f"{subject}@{now}",
            "text": note,
            "valid_from": now,
            "trust": 1.0,
            "last_used": now,
            **optional,
        },
        0,
    )


def _retire_overlay_blocks(overlay: str, replaces: str | None) -> tuple[list[str], int]:
    """Split the overlay into blocks and drop those contradicted by ``replaces``."""
    blocks = [block.strip() for block in overlay.split("\n\n") if block.strip()]
    if not replaces:
        return blocks, 0
    needle = replaces.lower()
    kept = [block for block in blocks if needle not in block.lower()]
    return kept, len(blocks) - len(kept)


def _is_walk_owned(row: dict[str, Any]) -> bool:
    return str(row.get("type", "")).startswith("AsterixDB ")
