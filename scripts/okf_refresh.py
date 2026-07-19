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
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# The pure reconcile core and canonical store constants are shared with the
# gateway's automatic startup walk (asterixdb_mcp.okf_walk); this script adds
# the full pipeline on top: grounding, revalidation, scoped refreshes.
from asterixdb_mcp.okf_walk import (  # noqa: E402
    BOOTSTRAP_STATEMENTS,
    CURRENT_ROWS_QUERY,
    KIND,
    SELF_DATAVERSE,
    WALK_QUERY,
    merge_layers,  # noqa: F401  (re-exported: tests and bundles import via this module)
    reconcile,
    reground_overlay,  # noqa: F401  (re-exported)
)
from asterixdb_mcp.okf_walk import (  # noqa: E402
    in_scope as _in_scope,
)
from asterixdb_mcp.okf_walk import (  # noqa: E402
    walk_owned as _walk_owned,  # noqa: F401  (re-exported)
)

# stamped on every statement the pipeline itself runs so the walk's workload
# mining never echoes pipeline plumbing back into the concept docs
PIPELINE_MARKER = "/*okf*/"
DATASET_CONCEPT_TYPES = ("AsterixDB Dataset", "AsterixDB External Dataset", "AsterixDB View")
MAX_ADVISED_STATEMENTS = 3


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
