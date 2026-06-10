"""Columnar full-scan advisory (Defense-in-Depth, plan layer).

A COLUMNAR dataset stores each field in its own column group. An unrestricted
scan with no projection reads every column of every row — the most expensive
thing you can do to a columnar store, and a frequent LLM mistake (``SELECT *``).
The tool descriptions warn against it (Layer 1); this is the plan-layer backstop
(Layer 2): it walks the optimized plan and detects a query whose only access
path on a columnar dataset is an unrestricted full scan.

It does NOT block the query. A valid SQL++ statement always runs — blocking a
shape the user legitimately asked for frustrates non-expert callers and pushes
LLM agents into retry loops. Instead this returns a non-fatal ``ColumnarAdvisory``
that the execute path attaches to the result and uses to minimize the output it
hands the LLM. The real infra guards (read-only, query timeout, statement LIMIT,
egress byte caps) stay in force regardless.

"Restricted" means the plan projects specific columns (a ``project`` operator) or
filters rows (a ``select`` operator). Either prunes the columnar read enough to
be acceptable. With neither, the scan is unrestricted and flagged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cc_client import CCClient
from .errors import GatewayError
from .plan_parser import ParsedPlan, datasets_from_sources, parse_optimized_plan
from .tools.get_schema import extract_dataset_format_info

_COLUMNAR = "COLUMNAR"
_RESTRICTING_OPERATORS = ("project", "select")
ADVISORY_TYPE = "COLUMNAR_FULL_SCAN"

_FORMAT_QUERY = (
    "SELECT VALUE d FROM Metadata.`Dataset` d "
    "WHERE d.DataverseName = $dv AND d.DatasetName = $ds;"
)


@dataclass(frozen=True)
class ColumnarAdvisory:
    """Non-fatal notice that a query ran an unrestricted scan of columnar dataset(s).

    Immutable. ``datasets`` holds the flagged ``Dataverse.Dataset`` names;
    ``message`` is the plain-English explanation surfaced to the caller.
    """

    datasets: tuple[str, ...]
    message: str

    def to_payload(self) -> dict[str, Any]:
        """Render to the ``advisories`` entry attached to a tool result."""
        return {
            "type": ADVISORY_TYPE,
            "datasets": list(self.datasets),
            "message": self.message,
        }


def _advisory_message(names: str) -> str:
    """Build the caller-facing explanation for a flagged columnar full scan."""
    return (
        f"Query scanned the entire COLUMNAR dataset(s) {names} with no column projection "
        "or filter. Columnar storage is expensive to read whole, so this is slower and "
        "costlier than needed. The query still ran and results are below, but the output "
        "was minimized to protect the context window. To run it faster and see more rows, "
        "project only the columns you need (SELECT col1, col2, ... not SELECT *) or add a "
        "WHERE filter. Call get_schema to see the available columns."
    )


def check_columnar_scan(
    parsed: ParsedPlan, columnar_full_names: set[str]
) -> ColumnarAdvisory | None:
    """Flag an unrestricted columnar scan; return None when the plan is safe.

    ``columnar_full_names`` holds the ``Dataverse.Dataset`` names known to be
    columnar. An advisory is returned only when the plan scans one of them AND
    applies no projection or filtering anywhere in the tree.
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
    ordered = tuple(sorted(scanned_columnar))
    return ColumnarAdvisory(datasets=ordered, message=_advisory_message(", ".join(ordered)))


async def assess_columnar_scan(
    client: CCClient,
    ccid: str,
    plans: Any,
    default_dataverse: str | None,
) -> ColumnarAdvisory | None:
    """Parse a compile-only plan and flag an unrestricted columnar full scan.

    Returns a ColumnarAdvisory when the plan is an unrestricted columnar scan, or
    None when the plan is safe (or has no parsable plan / no columnar datasets to
    protect). Never blocks execution.
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
