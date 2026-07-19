"""Materialize the OKF catalog bundle into the agentic-memory store.

The write path of the memory layer. The read-only MCP gateway never mutates
anything; this script talks to the cluster controller's query service directly
and:

1. bootstraps the ``AgentMemory.Memory`` store (idempotent DDL: dataset +
   subject B-tree + full-text index over the concept bodies),
2. walks the engine's ``okf_catalog()`` datasource function to get the current
   OKF concept bundle (one linked doc per dataverse/dataset/datatype/index),
3. reconciles it against the store **bi-temporally** by ``subject``:
   - unchanged concept -> row kept as-is,
   - changed concept   -> current row superseded (``valid_to`` stamped), new
     row inserted as current,
   - vanished concept  -> current row superseded,
   - new concept       -> inserted as current.

Superseded rows are never deleted: they are the concept's history (OKF's
``log.md`` analogue) and the input for drift analysis.

Each concept is **two layers reconciled by subject**: the deterministic *core*
(everything the walk and the grounding sweeps emit) and the learned *overlay*
(annotations distilled from conversations or imported from bundles). A re-walk
refreshes the core and never touches the overlay; overlay claims that
reference schema elements gone from the refreshed core no longer hold and are
dropped from the carried-forward overlay (the superseded row keeps them as
history). ``text`` is always the merged rendering, so the full-text index and
every consumer see one seamless document.

``--revalidate`` adds a self-grounding pass over stored memories the walk does
not itself refresh: each such row's ``source_query`` is re-executed and
fingerprinted. The conflict policy
is **tool-wins** — a row whose query now returns a different result is stale
memory and gets superseded; a failed/unreachable query proves nothing and
leaves the row alone.

Usage:
    python scripts/okf_refresh.py [--cc http://localhost:19002] [--dataverse DV]
                                  [--ground] [--revalidate]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

KIND = "semantic"
# the store itself must not be part of the knowledge it stores
SELF_DATAVERSE = "AgentMemory"
# stamped on every statement the pipeline itself runs so the walk's workload
# mining never echoes pipeline plumbing back into the concept docs
PIPELINE_MARKER = "/*okf*/"
DATASET_CONCEPT_TYPES = ("AsterixDB Dataset", "AsterixDB External Dataset", "AsterixDB View")
MAX_ADVISED_STATEMENTS = 3

BOOTSTRAP_STATEMENTS = (
    "CREATE DATAVERSE AgentMemory IF NOT EXISTS;",
    "CREATE TYPE AgentMemory.MemoryType IF NOT EXISTS AS OPEN { id: string };",
    "CREATE DATASET AgentMemory.Memory(MemoryType) IF NOT EXISTS PRIMARY KEY id;",
    "CREATE INDEX memSubject IF NOT EXISTS ON AgentMemory.Memory(subject: string?) ENFORCED;",
    "CREATE INDEX memText IF NOT EXISTS ON AgentMemory.Memory(`text`: string?) TYPE FULLTEXT ENFORCED;",
    # episodic query-outcome events the gateway records for distillation
    "CREATE TYPE AgentMemory.SessionEventType IF NOT EXISTS AS OPEN { id: string };",
    "CREATE DATASET AgentMemory.SessionEvent(SessionEventType) IF NOT EXISTS PRIMARY KEY id;",
)

WALK_QUERY = 'SET `import-private-functions` "true"; SELECT VALUE c FROM okf_catalog({args}) c;'
CURRENT_ROWS_QUERY = (
    'SELECT VALUE m FROM AgentMemory.Memory m WHERE m.kind = "{kind}" AND m.valid_to IS UNKNOWN;'
)


def execute(cc: str, statement: str) -> dict[str, Any]:
    """POST one statement to the CC query service; raise on non-success."""
    body = urllib.parse.urlencode({"statement": statement}).encode()
    request = urllib.request.Request(
        cc.rstrip("/") + "/query/service",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            envelope = json.load(response)
    except urllib.error.HTTPError as err:
        envelope = json.load(err)
    if envelope.get("status") != "success":
        raise RuntimeError(f"statement failed: {envelope.get('errors')}\n  {statement[:200]}")
    return envelope


def bootstrap(cc: str) -> None:
    for statement in BOOTSTRAP_STATEMENTS:
        execute(cc, statement)


def fetch_bundle(cc: str, dataverse: str | None) -> dict[str, dict[str, Any]]:
    """Walk okf_catalog() and key the emitted concept docs by subject."""
    args = json.dumps(dataverse) if dataverse else ""
    rows = execute(cc, WALK_QUERY.format(args=args)).get("results", [])
    return {
        row["subject"]: row
        for row in rows
        if isinstance(row, dict)
        and "subject" in row
        and not _in_scope(row["subject"], SELF_DATAVERSE)
    }


def fetch_current(cc: str) -> dict[str, dict[str, Any]]:
    rows = execute(cc, CURRENT_ROWS_QUERY.format(kind=KIND)).get("results", [])
    return {row["subject"]: row for row in rows if isinstance(row, dict) and "subject" in row}


_BACKTICKED = re.compile(r"`([^`]+)`")


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
        if subject in bundle or not _walk_owned(existing):
            continue
        if scope is None or _in_scope(subject, scope):
            supersede.append({**existing, "valid_to": now})
    return inserts, supersede, unchanged


def _walk_owned(row: dict[str, Any]) -> bool:
    """Only rows the catalog walk emits may be superseded as *vanished*.

    Imported or conversation-distilled concepts are never in the walk bundle,
    so without this guard every full refresh would supersede them wholesale.
    """
    return str(row.get("type", "")).startswith("AsterixDB ")


def _in_scope(subject: str, dataverse: str) -> bool:
    """A scoped refresh must only supersede that dataverse's vanished concepts."""
    return (
        subject == dataverse
        or subject.startswith(dataverse + ".")
        or subject.startswith(dataverse + "/")
    )


def apply(cc: str, inserts: list[dict[str, Any]], supersede: list[dict[str, Any]]) -> None:
    if supersede:
        execute(cc, f"UPSERT INTO AgentMemory.Memory ({json.dumps(supersede)});")
    if inserts:
        execute(cc, f"INSERT INTO AgentMemory.Memory ({json.dumps(inserts)});")


def ground(cc: str, bundle: dict[str, dict[str, Any]]) -> int:
    """Self-grounding pass: execute each dataset doc's profiling queries and fold
    the actual values into the doc before reconcile.

    Sections contain only values (no timestamps), so an unchanged database
    yields byte-identical docs and the refresh stays idempotent. Every statement
    is stamped with PIPELINE_MARKER so workload mining ignores it.
    """
    grounded = 0
    for doc in bundle.values():
        if doc.get("type") not in DATASET_CONCEPT_TYPES:
            continue
        sections: list[str] = []
        row_count = _grounded_rowcount(cc, doc)
        if row_count is not None:
            sections.append(f"- Row count (grounded): {row_count}")
            doc["grounded_rowcount"] = row_count
        advice = _grounded_advice(cc, doc)
        if advice:
            doc["recommended_indexes"] = advice
        if sections:
            doc["text"] += "\n# Grounded statistics\n\n" + "\n".join(sections) + "\n"
        if advice:
            doc["text"] += (
                "\n# Index advice\n\nFrom the native ADVISE advisor over observed queries:\n\n"
                + "".join(f"- `{ddl}`\n" for ddl in advice)
            )
        if sections or advice:
            grounded += 1
    return grounded


def _grounded_rowcount(cc: str, doc: dict[str, Any]) -> int | None:
    count_query = next(iter(doc.get("profile_queries") or []), None)
    if not count_query:
        return None
    try:
        results = execute(cc, f"{PIPELINE_MARKER} {count_query}").get("results", [])
    except RuntimeError:
        return None  # grounding is best-effort; the walk doc still lands
    if results and isinstance(results[0], dict) and isinstance(results[0].get("cnt"), int):
        return results[0]["cnt"]
    return None


def _grounded_advice(cc: str, doc: dict[str, Any]) -> list[str]:
    """Run the native ADVISE advisor over the doc's observed SELECT statements."""
    recommended: set[str] = set()
    statements = [
        s
        for s in (doc.get("observed_queries") or [])
        if s.lstrip().lower().startswith(("select", "with", "from"))
    ]
    for statement in statements[:MAX_ADVISED_STATEMENTS]:
        try:
            results = execute(cc, f"{PIPELINE_MARKER} ADVISE {statement}").get("results", [])
        except RuntimeError:
            continue  # cluster without ADVISE, or a non-advisable statement
        _collect_recommended(results, recommended, under_recommended=False)
    return sorted(recommended)


def _collect_recommended(node: Any, out: set[str], *, under_recommended: bool) -> None:
    """Walk an ADVISE result for index_statement strings under recommended_indexes."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "index_statement" and under_recommended and isinstance(value, str):
                out.add(value.strip())
            else:
                _collect_recommended(
                    value, out, under_recommended=under_recommended or "recommended" in key
                )
    elif isinstance(node, list):
        for item in node:
            _collect_recommended(item, out, under_recommended=under_recommended)


def _digest(results: Any) -> str:
    """Stable fingerprint of a query result for drift detection."""
    canonical = json.dumps(results, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def revalidate(
    cc: str, current: dict[str, dict[str, Any]], now: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Re-run each current row's ``source_query`` and compare fingerprints.

    Tool-wins conflict policy: a changed result means the memory is stale, so
    the row is superseded (bi-temporally — never deleted); the following walk
    or write re-inserts the corrected fact. Rows seen for the first time are
    stamped with their fingerprint in place. A failed or unreachable query
    proves nothing and leaves the row alone.

    Returns (rows_to_supersede, rows_to_stamp, checked_count).
    """
    supersede: list[dict[str, Any]] = []
    stamp: list[dict[str, Any]] = []
    checked = 0
    for row in current.values():
        query = row.get("source_query")
        if not isinstance(query, str) or not query.strip():
            continue
        checked += 1
        try:
            results = execute(cc, f"{PIPELINE_MARKER} {query}").get("results", [])
        except RuntimeError:
            continue
        digest = _digest(results)
        stored = row.get("grounding_digest")
        if stored is None:
            stamp.append({**row, "grounding_digest": digest})
        elif stored != digest:
            supersede.append({**row, "valid_to": now, "superseded_by": "revalidation"})
    return supersede, stamp, checked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default="http://localhost:19002", help="CC base URL")
    parser.add_argument("--dataverse", default=None, help="Refresh only this dataverse")
    parser.add_argument(
        "--ground",
        action="store_true",
        help="Execute each doc's profiling queries and ADVISE, folding results in",
    )
    parser.add_argument(
        "--revalidate",
        action="store_true",
        help="Re-run stored source_query fingerprints first; supersede drifted rows",
    )
    options = parser.parse_args()

    bootstrap(options.cc)
    now = datetime.now(timezone.utc).isoformat()
    bundle = fetch_bundle(options.cc, options.dataverse)
    grounded = ground(options.cc, bundle) if options.ground else 0
    current = fetch_current(options.cc)
    if options.revalidate:
        # only rows the walk does not refresh itself: the walk supersedes its
        # own subjects overlay-preservingly, so revalidating them here would
        # lose the carried overlay
        unmanaged = {s: row for s, row in current.items() if s not in bundle}
        stale, stamp, checked = revalidate(options.cc, unmanaged, now)
        apply(options.cc, [], stale + stamp)
        stale_ids = {row["id"] for row in stale}
        current = {s: row for s, row in current.items() if row.get("id") not in stale_ids}
        print(f"okf_refresh: revalidated {checked} | {len(stale)} drifted | {len(stamp)} stamped")
    inserts, supersede, unchanged = reconcile(bundle, current, now, options.dataverse)
    apply(options.cc, inserts, supersede)
    print(
        f"okf_refresh: {len(bundle)} concepts walked | "
        f"{len(inserts)} inserted | {len(supersede)} superseded | {unchanged} unchanged | "
        f"{grounded} grounded"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
