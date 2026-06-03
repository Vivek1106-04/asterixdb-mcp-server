"""Columnar plan-rejection guardrail (Defense-in-Depth, plan layer).

A COLUMNAR dataset stores each field in its own column group. An unrestricted
scan with no projection reads every column of every row — the most expensive
thing you can do to a columnar store, and a frequent LLM mistake (``SELECT *``).
The tool descriptions warn against it (Layer 1), but a model can still produce a
plan that does it. This guard is the plan-layer backstop (Layer 2): it walks the
optimized plan and rejects a query whose only access path on a columnar dataset
is an unrestricted full scan.

"Restricted" means the plan projects specific columns (a ``project`` operator) or
filters rows (a ``select`` operator). Either prunes the columnar read enough to
be acceptable. With neither, the scan is unrestricted and rejected with a
``PLAN_REJECTED`` error pointing the model at get_schema's column groups.
"""

from __future__ import annotations

from typing import Any

from .cc_client import CCClient
from .errors import ErrorType, GatewayError
from .plan_parser import ParsedPlan, datasets_from_sources, parse_optimized_plan
from .tools.get_schema import extract_dataset_format_info

_COLUMNAR = "COLUMNAR"
_RESTRICTING_OPERATORS = ("project", "select")

_FORMAT_QUERY = (
    "SELECT VALUE d FROM Metadata.`Dataset` d "
    "WHERE d.DataverseName = $dv AND d.DatasetName = $ds;"
)


def check_columnar_scan(
    parsed: ParsedPlan, columnar_full_names: set[str]
) -> GatewayError | None:
    """Reject an unrestricted columnar scan; return None when the plan is safe.

    ``columnar_full_names`` holds the ``Dataverse.Dataset`` names known to be
    columnar. The plan is rejected only when it scans one of them AND applies no
    projection or filtering anywhere in the tree.
    """
    if not columnar_full_names:
        return None
    scanned_columnar = {
        f"{dv}.{ds}"
        for dv, ds in datasets_from_sources(parsed.data_sources, None)
        if f"{dv}.{ds}" in columnar_full_names
    }
    if not scanned_columnar:
        return None
    restricted = any(op in parsed.operator_counts for op in _RESTRICTING_OPERATORS)
    if restricted:
        return None
    names = ", ".join(sorted(scanned_columnar))
    return GatewayError(
        ErrorType.PLAN_REJECTED,
        f"This query does an unrestricted full scan of COLUMNAR dataset(s) {names}. "
        "Columnar storage is expensive to scan whole: project the specific columns you "
        "need (SELECT col1, col2, ... not SELECT *) and/or add a WHERE filter. Call "
        "get_schema to see the available columns.",
    )


async def enforce_columnar_safety(
    client: CCClient,
    ccid: str,
    plans: Any,
    default_dataverse: str | None,
) -> GatewayError | None:
    """Parse a compile-only plan and reject an unrestricted columnar scan.

    Returns a PLAN_REJECTED GatewayError to block execution, or None when the
    plan is safe (or has no parsable plan / no columnar datasets to protect).
    """
    parsed = parse_optimized_plan(plans)
    if parsed is None:
        return None
    datasets = datasets_from_sources(parsed.data_sources, default_dataverse)
    if not datasets:
        return None
    columnar = await _columnar_datasets(client, ccid, datasets)
    return check_columnar_scan(parsed, columnar)


async def _columnar_datasets(
    client: CCClient, ccid: str, datasets: list[tuple[str, str]]
) -> set[str]:
    """Return the ``Dataverse.Dataset`` names (of the given pairs) that are COLUMNAR."""
    columnar: set[str] = set()
    for dv, ds in datasets:
        try:
            envelope = await client.execute(
                _FORMAT_QUERY, client_context_id=ccid, statement_parameters={"dv": dv, "ds": ds}
            )
        except GatewayError:
            continue
        rows = [r for r in (envelope.get("results") or []) if isinstance(r, dict)]
        if rows and extract_dataset_format_info(rows[0]).get("format") == _COLUMNAR:
            columnar.add(f"{dv}.{ds}")
    return columnar
