"""The OKF catalog walk: materialize okf_catalog() into the memory store.

The pure reconcile core lives here so two callers can share it:

- ``scripts/okf_refresh.py`` — the full manual pipeline (adds grounding,
  revalidation, scoped refreshes),
- the gateway's startup maintenance (``maintenance.py``) — a light automatic
  walk that keeps the store in sync with the catalog with no operator effort.

Layer model: a concept row keeps the machine-derived ``core`` and the learned
``overlay`` apart, with ``text`` as the merged rendering. The walk owns the
core; overlays survive every re-walk, re-grounded against the new core so
claims about vanished schema elements retire with their evidence intact
(superseded rows keep them as history).

Security posture: every write goes through ``CCClient.execute_memory_write``
(INSERT/UPSERT into the AgentMemory store only) with each row bound as a
statement parameter — no row content is ever spliced into statement text. The
walk query itself is read-only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .cc_client import CCClient
from .config import Settings
from .context_id import make_client_context_id
from .memory_store import (
    BOOTSTRAP_STATEMENTS,
    MEMORY_DATASET,
    SELF_DATAVERSE,
    scope_clause,
)

# Both are part of this module's public surface historically; re-exported so
# existing importers keep working now that the store owns them.
__all__ = ["BOOTSTRAP_STATEMENTS", "SELF_DATAVERSE"]

KIND = "semantic"

# Stamped on every statement this pipeline runs against the walked datasets
# (grounding counts, ADVISE), and handed to okf_catalog() as its exclude_marker
# so the engine's workload mining never echoes our own plumbing back into the
# concept docs. Without it every refresh would find its own last refresh in the
# workload, change the doc, and supersede the row for no real reason.
PIPELINE_MARKER = "/*okf*/"

WALK_QUERY = 'SET `import-private-functions` "true"; SELECT VALUE c FROM okf_catalog({args}) c;'
CURRENT_ROWS_QUERY = (
    f"SELECT VALUE m FROM {MEMORY_DATASET} m "
    f'WHERE m.kind = "{{kind}}" AND m.valid_to IS UNKNOWN AND {scope_clause("m")};'
)

_UPSERT_ROW = f"UPSERT INTO {MEMORY_DATASET} ([$row]);"
_INSERT_ROW = f"INSERT INTO {MEMORY_DATASET} ([$row]);"

_BACKTICKED = re.compile(r"`([^`]+)`")


def walk_args(dataverse: str | None) -> str:
    """Render okf_catalog()'s arguments: which dataverse, and our own marker.

    An empty first argument walks the whole catalog, which is how the engine
    lets a caller ask for everything while still supplying an exclude marker.
    """
    return f"{json.dumps(dataverse or '')}, {json.dumps(PIPELINE_MARKER)}"


# pure reconcile core (shared with scripts/okf_refresh.py)


def merge_layers(core: str, overlay: str) -> str:
    """Render one concept document from its two layers; the split is invisible."""
    if not overlay:
        return core
    return core.rstrip("\n") + "\n\n" + overlay.rstrip("\n") + "\n"


def reground_overlay(overlay: str, old_core: str, new_core: str) -> tuple[str, list[str]]:
    """Re-ground overlay claims against a refreshed core.

    A claim (line) that backtick-references a schema element which existed in
    the old core but is gone from the new core no longer holds and is dropped;
    the superseded row keeps it as history. References that never resolved
    against the core (business terms, external names) are left alone.

    Returns (kept_overlay, dropped_lines).
    """
    if not overlay:
        return "", []
    kept: list[str] = []
    dropped: list[str] = []
    for line in overlay.splitlines():
        stale = [
            ref for ref in _BACKTICKED.findall(line) if ref in old_core and ref not in new_core
        ]
        (dropped if stale else kept).append(line)
    text = "\n".join(kept).strip("\n")
    return (text + "\n" if text else ""), dropped


def reconcile(
    bundle: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    now: str,
    scope: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Pure reconcile: returns (rows_to_insert, rows_to_supersede, unchanged_count).

    Layer-aware: an incoming doc's ``text`` (or explicit ``core``) is the new
    deterministic core. Walk docs carry no ``overlay`` key, so the stored
    overlay is carried forward, re-grounded against the new core; import docs
    carry an explicit ``overlay``. Stored rows keep ``core`` and ``overlay``
    apart, with ``text`` as the merged rendering. Pre-layering rows (no
    ``core`` field) fall back to comparing ``text``.
    """
    inserts: list[dict[str, Any]] = []
    supersede: list[dict[str, Any]] = []
    unchanged = 0

    for subject, doc in bundle.items():
        existing = current.get(subject)
        new_core = str(doc.get("core", doc.get("text", "")))
        incoming_overlay = doc.get("overlay")
        if existing is not None:
            old_core = str(existing.get("core") or existing.get("text", ""))
            old_overlay = str(existing.get("overlay") or "")
            if incoming_overlay is None:
                overlay, _ = reground_overlay(old_overlay, old_core, new_core)
            else:
                overlay = str(incoming_overlay)
            if new_core == old_core and overlay == old_overlay:
                unchanged += 1
                continue
            supersede.append({**existing, "valid_to": now})
        else:
            overlay = str(incoming_overlay or "")
        row = {key: value for key, value in doc.items() if key not in ("core", "overlay", "text")}
        row.update(
            id=f"{subject}@{now}",
            kind=KIND,
            valid_from=now,
            core=new_core,
            text=merge_layers(new_core, overlay),
        )
        if overlay:
            row["overlay"] = overlay
        inserts.append(row)

    for subject, existing in current.items():
        if subject in bundle or not walk_owned(existing):
            continue
        if scope is None or in_scope(subject, scope):
            supersede.append({**existing, "valid_to": now})
    return inserts, supersede, unchanged


def walk_owned(row: dict[str, Any]) -> bool:
    """Only rows the catalog walk emits may be superseded as *vanished*.

    Imported or conversation-distilled concepts are never in the walk bundle,
    so without this guard every full refresh would supersede them wholesale.
    """
    return str(row.get("type", "")).startswith("AsterixDB ")


def in_scope(subject: str, dataverse: str) -> bool:
    """A scoped refresh must only supersede that dataverse's vanished concepts."""
    return (
        subject == dataverse
        or subject.startswith(dataverse + ".")
        or subject.startswith(dataverse + "/")
    )


# async walk (gateway-side)


async def fetch_bundle(client: CCClient, ccid: str) -> dict[str, dict[str, Any]]:
    """Walk okf_catalog() and key the emitted concept docs by subject."""
    envelope = await client.execute(WALK_QUERY.format(args=walk_args(None)), client_context_id=ccid)
    return {
        row["subject"]: row
        for row in envelope.get("results", [])
        if isinstance(row, dict)
        and "subject" in row
        and not in_scope(row["subject"], SELF_DATAVERSE)
    }


async def fetch_current(client: CCClient, ccid: str) -> dict[str, dict[str, Any]]:
    """Current walk-kind rows in the store, keyed by subject."""
    envelope = await client.execute_memory_read(
        CURRENT_ROWS_QUERY.format(kind=KIND), client_context_id=ccid
    )
    return {
        row["subject"]: row
        for row in envelope.get("results", [])
        if isinstance(row, dict) and "subject" in row
    }


async def bootstrap_store(client: CCClient, ccid: str) -> None:
    """Create the AgentMemory store objects if absent (idempotent, allowlisted)."""
    for statement in BOOTSTRAP_STATEMENTS:
        await client.execute_memory_write(statement, client_context_id=ccid)


async def run_walk(client: CCClient, settings: Settings) -> dict[str, int]:
    """One automatic catalog walk: reconcile okf_catalog() into the store.

    Idempotent — an unchanged catalog writes nothing. Requires the engine's
    okf_catalog() function; a cluster without it raises GatewayError, which the
    caller treats as "walk unavailable" and degrades.
    """
    ccid = make_client_context_id(settings.agent_session_id, "okf_walk")
    bundle = await fetch_bundle(client, ccid)
    current = await fetch_current(client, ccid)
    now = datetime.now(timezone.utc).isoformat()
    inserts, supersede, unchanged = reconcile(bundle, current, now, scope=None)
    for row in supersede:
        await client.execute_memory_write(
            _UPSERT_ROW, client_context_id=ccid, statement_parameters={"row": row}
        )
    for row in inserts:
        await client.execute_memory_write(
            _INSERT_ROW, client_context_id=ccid, statement_parameters={"row": row}
        )
    return {
        "concepts": len(bundle),
        "inserted": len(inserts),
        "superseded": len(supersede),
        "unchanged": unchanged,
    }


__all__ = [
    "BOOTSTRAP_STATEMENTS",
    "CURRENT_ROWS_QUERY",
    "KIND",
    "PIPELINE_MARKER",
    "SELF_DATAVERSE",
    "WALK_QUERY",
    "bootstrap_store",
    "fetch_bundle",
    "fetch_current",
    "in_scope",
    "merge_layers",
    "reconcile",
    "reground_overlay",
    "run_walk",
    "walk_args",
    "walk_owned",
]
