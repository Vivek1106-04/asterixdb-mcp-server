"""database_health_check: pure-metadata advisory scan of the catalog.

A read-only, compile-free health pass over the metadata catalog. It reads the
Dataset and Index collections and reports schema-level issues an agent (or
operator) can act on without running any workload:

- DUPLICATE_INDEX           — two secondary indexes on one dataset with the same
                              structure and identical key fields. The second is
                              dead weight: extra write cost, no read benefit.
- REDUNDANT_INDEX           — a secondary index whose key fields are a strict
                              prefix of another index of the same structure on
                              the same dataset; the longer index already covers it.
- ROW_DATASET_COLUMNAR_CANDIDATE — an internal dataset stored ROW that could be
                              a candidate for COLUMNAR storage for analytical
                              scans. Advisory only — the right choice depends on
                              the access pattern, which metadata alone cannot show.

This deliberately covers only what pure metadata can prove. Workload-driven
findings (un-indexed filtered fields, unused indexes) need a query history and
belong to get_query_history / the recommend_indexes prompt, not here.

Defense-in-Depth:
- Layer 1: the schema enumerates exactly which checks run and that the scan is
  metadata-only and read-only.
- Layer 2: the metadata fetch degrades to an empty finding set on a per-collection
  failure rather than failing the whole scan; the system Metadata dataverse is
  never reported on.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import GatewayError
from ..inventory import fetch_dataset_rows
from . import ToolResult
from .get_schema import extract_dataset_format_info

# The system catalog dataverse is never a target of health advice.
_SYSTEM_DATAVERSE = "Metadata"

# Finding severities, ordered most- to least- actionable.
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

# Stable check identifiers surfaced in each finding and in the `checks` list.
CHECK_DUPLICATE_INDEX = "DUPLICATE_INDEX"
CHECK_REDUNDANT_INDEX = "REDUNDANT_INDEX"
CHECK_COLUMNAR_CANDIDATE = "ROW_DATASET_COLUMNAR_CANDIDATE"

CHECKS = (CHECK_DUPLICATE_INDEX, CHECK_REDUNDANT_INDEX, CHECK_COLUMNAR_CANDIDATE)

_ALL_INDEXES_QUERY = "SELECT VALUE i FROM Metadata.`Index` i;"


async def run_database_health_check(
    client: CCClient,
    settings: Settings,
    *,
    dataverse: str | None = None,
) -> ToolResult:
    """Scan catalog metadata and report schema-level health findings."""
    ccid = make_client_context_id(settings.agent_session_id, "database_health_check")
    try:
        dataset_rows = await fetch_dataset_rows(client, ccid=ccid)
        index_rows = await _fetch_index_rows(client, ccid)
    except GatewayError as err:
        return ToolResult.error(err)

    datasets = _scope(dataset_rows, dataverse)
    indexes = _scope(index_rows, dataverse)

    findings = assemble_findings(datasets, indexes)
    structured: dict[str, Any] = {
        "status": "success",
        "dataverseFilter": dataverse,
        "datasetsScanned": len(datasets),
        "indexesScanned": len(indexes),
        "findingsCount": len(findings),
        "checks": list(CHECKS),
        "findings": findings,
    }
    return ToolResult(text=_summarize(structured), structured=structured)


# pure analysis (unit-tested without I/O)


def assemble_findings(
    datasets: list[dict[str, Any]], indexes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run every check and return findings ordered by severity then location."""
    findings = [
        *find_duplicate_indexes(indexes),
        *find_redundant_indexes(indexes),
        *find_columnar_candidates(datasets),
    ]
    rank = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}
    findings.sort(
        key=lambda f: (
            rank.get(f["severity"], 99),
            str(f.get("dataverse")),
            str(f.get("dataset")),
            f["check"],
        )
    )
    return findings


def find_duplicate_indexes(indexes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag secondary indexes on one dataset sharing structure and key fields."""
    findings: list[dict[str, Any]] = []
    for (dv, ds), group in _group_secondary_indexes(indexes).items():
        by_signature: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        for idx in group:
            signature = (idx["structure"], tuple(idx["keys"]))
            by_signature.setdefault(signature, []).append(idx["name"])
        for (structure, keys), names in by_signature.items():
            if len(names) < 2:
                continue
            ordered = sorted(names)
            findings.append(
                {
                    "check": CHECK_DUPLICATE_INDEX,
                    "severity": SEVERITY_HIGH,
                    "dataverse": dv,
                    "dataset": ds,
                    "indexes": ordered,
                    "indexType": structure,
                    "keyFields": list(keys),
                    "message": (
                        f"Indexes {', '.join(ordered)} on {dv}.{ds} are duplicates "
                        f"({structure} on {list(keys)}). Keep one and drop the rest to "
                        "cut write and storage cost."
                    ),
                }
            )
    return findings


def find_redundant_indexes(indexes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag an index whose keys are a strict prefix of a longer same-structure index."""
    findings: list[dict[str, Any]] = []
    for (dv, ds), group in _group_secondary_indexes(indexes).items():
        for shorter in group:
            covered_by = _prefix_cover(shorter, group)
            if covered_by is None:
                continue
            findings.append(
                {
                    "check": CHECK_REDUNDANT_INDEX,
                    "severity": SEVERITY_MEDIUM,
                    "dataverse": dv,
                    "dataset": ds,
                    "index": shorter["name"],
                    "coveredBy": covered_by["name"],
                    "indexType": shorter["structure"],
                    "keyFields": list(shorter["keys"]),
                    "message": (
                        f"Index {shorter['name']} on {dv}.{ds} ({list(shorter['keys'])}) "
                        f"is a key-prefix of {covered_by['name']} "
                        f"({list(covered_by['keys'])}); the longer index can serve the "
                        "same prefix lookups, so the shorter one may be redundant."
                    ),
                }
            )
    return findings


def find_columnar_candidates(datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag internal ROW datasets as candidates to evaluate for COLUMNAR storage."""
    findings: list[dict[str, Any]] = []
    for row in datasets:
        if str(row.get("DatasetType", "")).upper() != "INTERNAL":
            continue
        if extract_dataset_format_info(row).get("format") != "ROW":
            continue
        dv = row.get("DataverseName")
        ds = row.get("DatasetName")
        findings.append(
            {
                "check": CHECK_COLUMNAR_CANDIDATE,
                "severity": SEVERITY_LOW,
                "dataverse": dv,
                "dataset": ds,
                "message": (
                    f"{dv}.{ds} is stored ROW. If it is queried with analytical scans over "
                    "a few columns, COLUMNAR storage can cut I/O. Advisory only — weigh it "
                    "against point-lookup and write patterns before recreating the dataset."
                ),
            }
        )
    return findings


# helpers


def _prefix_cover(
    shorter: dict[str, Any], group: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return a longer same-structure index whose keys start with shorter's keys.

    Group entries are pre-filtered to have non-empty keys, so ``shorter["keys"]``
    is always populated here.
    """
    keys = shorter["keys"]
    for other in group:
        if other["name"] == shorter["name"] or other["structure"] != shorter["structure"]:
            continue
        if len(other["keys"]) > len(keys) and other["keys"][: len(keys)] == keys:
            return other
    return None


def _group_secondary_indexes(
    indexes: list[dict[str, Any]],
) -> dict[tuple[Any, Any], list[dict[str, Any]]]:
    """Group normalized secondary indexes by (dataverse, dataset)."""
    grouped: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for idx in indexes:
        if idx.get("IsPrimary"):
            continue
        keys = _normalize_keys(idx.get("SearchKey"))
        if not keys:
            continue
        key = (idx.get("DataverseName"), idx.get("DatasetName"))
        grouped.setdefault(key, []).append(
            {
                "name": idx.get("IndexName"),
                "structure": str(idx.get("IndexStructure", "")),
                "keys": keys,
            }
        )
    return grouped


def _normalize_keys(search_key: Any) -> list[str]:
    """Render an index SearchKey to a list of dotted field paths for comparison.

    Each key entry is itself a list of path segments (e.g. [["address","city"]]
    -> "address.city"); a bare string is taken verbatim.
    """
    if not isinstance(search_key, list):
        return []
    out: list[str] = []
    for entry in search_key:
        if isinstance(entry, list):
            out.append(".".join(str(seg) for seg in entry))
        else:
            out.append(str(entry))
    return out


def _scope(rows: list[dict[str, Any]], dataverse: str | None) -> list[dict[str, Any]]:
    """Drop system-catalog rows and, if given, restrict to one dataverse."""
    scoped: list[dict[str, Any]] = []
    for row in rows:
        dv = row.get("DataverseName")
        if dv == _SYSTEM_DATAVERSE:
            continue
        if dataverse is not None and dv != dataverse:
            continue
        scoped.append(row)
    return scoped


async def _fetch_index_rows(client: CCClient, ccid: str) -> list[dict[str, Any]]:
    """Fetch every Metadata.`Index` record as raw dicts."""
    envelope = await client.execute(_ALL_INDEXES_QUERY, client_context_id=ccid)
    rows = envelope.get("results") or []
    return [r for r in rows if isinstance(r, dict)]


def _summarize(structured: dict[str, Any]) -> str:
    """One-line human summary for the ``content`` text block."""
    n = structured["findingsCount"]
    scope = structured["dataverseFilter"] or "all dataverses"
    if n == 0:
        return (
            f"Health check on {scope}: no schema-level issues found across "
            f"{structured['datasetsScanned']} dataset(s) and "
            f"{structured['indexesScanned']} index(es)."
        )
    return (
        f"Health check on {scope}: {n} finding(s) across "
        f"{structured['datasetsScanned']} dataset(s) and "
        f"{structured['indexesScanned']} index(es). See findings for severity and fixes."
    )
