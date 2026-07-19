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

from datetime import datetime, timezone
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError
from . import ToolResult
from .memory_search import _IDENTIFIER_RE, MEMORY_DATASET

MAX_TEXT_LEN = 4000
MAX_LINKS = 16
NOTE_TYPE = "Note"
KIND = "semantic"

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
) -> ToolResult:
    """Persist one agent-curated note, reconciled bi-temporally by subject."""
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
    try:
        envelope = await client.execute(
            _CURRENT_QUERY, client_context_id=ccid, statement_parameters={"subject": subject}
        )
        rows = [row for row in envelope.get("results", []) if isinstance(row, dict)]
        existing = rows[0] if rows else None
        now = datetime.now(timezone.utc).isoformat()

        action, row, retired = _reconcile(
            existing, subject, note, now, links, tags, source_query, replaces
        )
        if action == "unchanged":
            return ToolResult(
                text=f"Memory for '{subject}' already contains this note; nothing written.",
                structured={
                    "status": "success",
                    "subject": subject,
                    "action": action,
                    "id": None,
                    "retired": 0,
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
    return ToolResult(
        text=f"Memory {action}: '{subject}' ({row['id']}){retired_text}."
        + _verification_guidance(source_query, retired),
        structured={
            "status": "success",
            "subject": subject,
            "action": action,
            "id": row["id"],
            "retired": retired,
            "verified": bool(source_query),
        },
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
