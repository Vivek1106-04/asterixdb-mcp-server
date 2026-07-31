"""Decay pass: archive standalone notes that never earned their keep.

A note written without grounding evidence that no recall ever delivered within
``DECAY_AFTER_DAYS`` is dead weight — it competes for the attach budget and
context window without ever proving useful. The decay pass retires such rows
bi-temporally (``valid_to`` stamped, ``archived_reason`` recorded): nothing is
deleted, history keeps the note, and re-writing the fact any time revives it.

Scope is deliberately narrow:

- only standalone ``Note`` rows — walk-owned catalog concepts are the store's
  backbone and never decay, and overlay annotations live inside them;
- only unverified rows — anything with a ``source_query`` is grounded
  knowledge and is revalidated, not aged out;
- only rows that were never recalled — one delivery resets the clock via
  ``last_recalled_at``;
- a row whose timestamps cannot be parsed is left alone: silent archival on
  malformed data would be data loss by another name.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .cc_client import CCClient
from .config import Settings
from .context_id import make_client_context_id
from .memory_store import MEMORY_DATASET

DECAY_AFTER_DAYS = 30.0

NOTE_TYPE = "Note"
_CANDIDATES_QUERY = (
    f'SELECT VALUE m FROM {MEMORY_DATASET} m WHERE m.valid_to IS UNKNOWN AND m.`type` = "Note";'
)
_ARCHIVE_UPSERT = f"UPSERT INTO {MEMORY_DATASET} ([$row]);"


def is_decay_candidate(row: dict[str, Any], now: datetime) -> bool:
    """Pure predicate: should this current row be archived by the decay pass?"""
    if row.get("type") != NOTE_TYPE or row.get("source_query"):
        return False
    if int(row.get("recall_count") or 0) > 0:
        return False
    stamp = row.get("last_recalled_at") or row.get("valid_from")
    try:
        then = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then) > timedelta(days=DECAY_AFTER_DAYS)


async def run_decay(client: CCClient, settings: Settings) -> dict[str, int]:
    """One decay pass over current standalone notes; returns summary counters."""
    ccid = make_client_context_id(settings.agent_session_id, "decay")
    envelope = await client.execute(_CANDIDATES_QUERY, client_context_id=ccid)
    rows = [row for row in envelope.get("results", []) if isinstance(row, dict)]
    now = datetime.now(timezone.utc)
    archived = 0
    for row in rows:
        if not is_decay_candidate(row, now):
            continue
        stamped = {
            **row,
            "valid_to": now.isoformat(),
            "archived_reason": (
                f"unverified and never recalled within {int(DECAY_AFTER_DAYS)} days"
            ),
        }
        await client.execute_memory_write(
            _ARCHIVE_UPSERT, client_context_id=ccid, statement_parameters={"row": stamped}
        )
        archived += 1
    return {"candidates": len(rows), "archived": archived}
