"""Adopt memory rows written before the store had owners.

Every read of the memory store now carries a tenant predicate, and a row with no
``principal`` field satisfies neither half of it: not the caller's principal, not
the global tier. Left alone, a store that predates tenant scoping would appear
empty on the first boot after the upgrade — the rows are all still there, and
none of them would ever be returned.

This pass finds those rows and rewrites them under the deployment's own
principal, which on a single-tenant gateway is the principal every request
already resolves to. It is idempotent by construction: the query matches only
rows that have no owner, so a second run finds nothing.

The stamping itself is not done here. Rows go back through the ordinary memory
write path, which owns them to the client's bound tenant — the same rule the
rest of the gateway writes under, with no second implementation to drift.
"""

from __future__ import annotations

import logging
from typing import Any

from .cc_client import CCClient
from .config import Settings
from .context_id import make_client_context_id
from .memory_store import (
    MEMORY_DATASET,
    SESSION_EVENT_DATASET,
    UNOWNED_EVENT_QUERY,
    UNOWNED_MEMORY_QUERY,
)

logger = logging.getLogger(__name__)

_ADOPT_MEMORY = f"UPSERT INTO {MEMORY_DATASET} ([$row]);"
_ADOPT_EVENT = f"UPSERT INTO {SESSION_EVENT_DATASET} ([$row]);"


async def backfill_principals(client: CCClient, settings: Settings) -> dict[str, int]:
    """Give every unowned row an owner. Returns how many of each were adopted."""
    if not settings.memory_write_enabled:
        # Writes are the only way to adopt a row, so there is nothing to do but
        # say why: a read-only gateway keeps its legacy rows and cannot see them
        # until an operator enables writes once.
        logger.info("memory writes are disabled; rows written before tenant scoping stay hidden")
        return {"concepts": 0, "events": 0}

    ccid = make_client_context_id(settings.agent_session_id, "backfill")
    concepts = await _adopt(client, ccid, UNOWNED_MEMORY_QUERY, _ADOPT_MEMORY)
    events = await _adopt(client, ccid, UNOWNED_EVENT_QUERY, _ADOPT_EVENT)
    if concepts or events:
        logger.info("adopted %s unowned concept(s) and %s unowned event(s)", concepts, events)
    return {"concepts": concepts, "events": events}


async def _adopt(client: CCClient, ccid: str, query: str, upsert: str) -> int:
    """Rewrite every row the query returns so the write path stamps its owner."""
    envelope = await client.execute(query, client_context_id=ccid)
    rows: list[dict[str, Any]] = [
        row for row in envelope.get("results", []) if isinstance(row, dict)
    ]
    for row in rows:
        await client.execute_memory_write(
            upsert, client_context_id=ccid, statement_parameters={"row": row}
        )
    return len(rows)
