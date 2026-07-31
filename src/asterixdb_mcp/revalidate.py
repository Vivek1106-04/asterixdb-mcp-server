"""Proof replay: let a note's own evidence retire it.

The staleness detector already finds contradictions — it holds a stored claim and
a fresh result at the same instant and says which number disagrees. What it never
did was write anything down, on the principle that the gateway must not invalidate
knowledge on its own inference.

That principle held, and the memory layer still did not self-heal. Across three
sessions run against deliberately mutated data, with the disagreement named
directly in the response, the model produced **zero** corrective writes every
time. It reads the warning, uses the fresh number for the answer it is giving, and
moves on. The stale note is still there for the next session, and for the session
after that. Relying on the agent to fix the store is relying on the one participant
with no incentive to.

So the write has to be the gateway's. This module is the one case where it can be
made without inference, because the note nominated its own falsifier: a grounded
note carries the ``source_query`` that proved it, and re-running that query is not
the gateway guessing. If the note's own proof now contradicts the note, the claim
is retired — bi-temporally, so the disproved version stays readable as history.

Three limits keep that from becoming its own hazard:

* **Only grounded notes.** No ``source_query``, no proof to replay, no retirement.
  Ungrounded notes stay in the flag-only regime.
* **A proof that cannot run is not disproof.** A query that errors — dropped
  dataset, syntax the engine no longer accepts — leaves an unknown, not a
  refutation, and retiring on unknowns would delete good knowledge every time the
  cluster hiccups. Those notes are flagged suspect instead.
* **Retire, never rewrite.** The gateway removes a claim its own evidence refutes.
  It does not invent the replacement; writing the new fact is still the job of
  whoever observes it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .cc_client import CCClient
from .config import Settings
from .context_id import make_client_context_id
from .errors import GatewayError
from .memory_store import MEMORY_DATASET, scope_clause
from .staleness import flag_suspect, note_conflicts

logger = logging.getLogger(__name__)

# Each replay is a real query against the cluster, so a pass is bounded by how
# much cluster time we are willing to spend on housekeeping rather than by how
# many notes happen to exist.
REVALIDATE_LIMIT = 50

NOTE_TYPE = "Note"
MAX_REASON_LEN = 500

_CANDIDATES_QUERY = (
    f"SELECT VALUE m FROM {MEMORY_DATASET} m "
    f'WHERE m.valid_to IS UNKNOWN AND m.`type` = "{NOTE_TYPE}" '
    f"AND m.source_query IS NOT UNKNOWN AND {scope_clause('m')} "
    f"LIMIT {REVALIDATE_LIMIT};"
)
_SUPERSEDE = f"UPSERT INTO {MEMORY_DATASET} ([$row]);"


def is_replayable(row: dict[str, Any]) -> bool:
    """Whether this row carries a proof that can be re-run against it."""
    if row.get("valid_to") is not None:
        return False
    if row.get("type") != NOTE_TYPE:
        return False
    return bool(row.get("source_query"))


async def run_revalidation(client: CCClient, settings: Settings) -> dict[str, int]:
    """Replay grounded notes against their own proofs; retire the refuted ones."""
    counters = {"checked": 0, "retired": 0, "unprovable": 0}
    if not settings.memory_write_enabled:
        # The point of replay is the corrective write. Without it this would only
        # re-run queries and discard the answers, which is pure cost.
        return counters

    ccid = make_client_context_id(settings.agent_session_id, "revalidate")
    try:
        envelope = await client.execute_memory_read(_CANDIDATES_QUERY, client_context_id=ccid)
    except GatewayError:
        return counters

    for row in envelope.get("results", []):
        if not isinstance(row, dict) or not is_replayable(row):
            continue
        counters["checked"] += 1
        await _replay_one(client, ccid, row, counters)
    if counters["retired"]:
        logger.info("revalidation retired %s note(s) their own proof refuted", counters["retired"])
    return counters


async def _replay_one(
    client: CCClient, ccid: str, row: dict[str, Any], counters: dict[str, int]
) -> None:
    """Re-run one note's proof and act on what comes back."""
    rows = await _run_proof(client, ccid, str(row["source_query"]))
    if rows is None:
        counters["unprovable"] += 1
        await _write(client, ccid, flag_suspect(row, ["its proving query no longer runs"]))
        return
    if not rows:
        # No rows is not a refutation: the query matched nothing, which is what a
        # mid-migration dataset returns and what a filtered proof legitimately
        # returns. Silence is not evidence.
        return

    conflicts = note_conflicts(str(row.get("text", "")), rows)
    if not conflicts:
        return

    counters["retired"] += 1
    await _write(client, ccid, _retired(row, conflicts))


async def _run_proof(client: CCClient, ccid: str, source_query: str) -> list[Any] | None:
    """The proof's current result, or None when the proof itself could not run."""
    try:
        envelope = await client.execute(source_query, client_context_id=ccid)
    except GatewayError:
        return None
    results = envelope.get("results")
    return results if isinstance(results, list) else None


def _retired(row: dict[str, Any], conflicts: list[str]) -> dict[str, Any]:
    """The superseded form of a note its own proof refuted."""
    reason = "refuted by its own source_query: " + "; ".join(conflicts)
    return {
        **row,
        "valid_to": datetime.now(timezone.utc).isoformat(),
        "archived_reason": reason[:MAX_REASON_LEN],
    }


async def _write(client: CCClient, ccid: str, row: dict[str, Any]) -> None:
    """Persist one revalidation outcome; a failed write never fails the pass."""
    try:
        await client.execute_memory_write(
            _SUPERSEDE, client_context_id=ccid, statement_parameters={"row": row}
        )
    except GatewayError:
        return
