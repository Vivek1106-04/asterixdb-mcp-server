"""get_dataset_statistics: a dataset's scale and statistics freshness.

Reports the cost-based optimizer's view of a dataset — its estimated row count
and average document size, hence an estimated total size — so an agent can judge
how large a query will be BEFORE running it (pick a target, add a LIMIT, expect a
full scan to be expensive). Those estimates come from the last ``ANALYZE
DATASET``, recorded as a ``SAMPLE`` index in the metadata catalog.

When a dataset has never been analyzed it has no sample, so no estimates exist —
the tool reports ``analyzed: false`` and tells the caller that the optimizer (and
recommend_indexes) is planning that dataset without statistics. ``ANALYZE`` is a
write; the read-only gateway never runs it, it only reports whether it is needed.

Defense-in-Depth:
- Layer 1: the schema states the estimates come from ANALYZE and may be stale or
  absent, and that the gateway will not run ANALYZE.
- Layer 2: a dataset with no sample yields a self-correcting ``analyzed: false``
  result with the exact ``ANALYZE DATASET`` statement to run, never an error.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError
from ..sample_stats import fetch_dataset_stats
from . import ToolResult


async def run_get_dataset_statistics(
    client: CCClient,
    settings: Settings,
    *,
    dataverse: str,
    dataset: str,
    user_tag: str | None = None,
) -> ToolResult:
    """Return a dataset's sampled row-count/size estimates and analyzed state."""
    dv, ds = dataverse.strip(), dataset.strip()
    if not dv or not ds:
        return ToolResult.error(
            GatewayError(
                ErrorType.INVALID_PARAMETER,
                "Provide both a dataverse and a dataset name.",
            )
        )

    ccid = make_client_context_id(settings.agent_session_id, user_tag)
    # fetch_dataset_stats degrades to None on any read failure (treated as
    # "not analyzed"), so there is no error path to forward here.
    stats = await fetch_dataset_stats(client, ccid, dataverse=dv, dataset=ds)

    structured: dict[str, Any] = {
        "status": "success",
        "dataverse": dv,
        "dataset": ds,
        "analyzed": stats is not None,
    }
    if stats is None:
        structured["analyzeStatement"] = f"ANALYZE DATASET {dv}.{ds};"
        structured["note"] = (
            f"{dv}.{ds} has no sample statistics: it has either never been analyzed or "
            "does not exist. Row-count and size estimates are unavailable, and the "
            "cost-based optimizer (and recommend_indexes) plans this dataset without "
            "statistics. Run the analyzeStatement to populate them (the gateway is "
            "read-only and will not run it)."
        )
        return ToolResult(
            text=f"{dv}.{ds} has no sample statistics (not analyzed).", structured=structured
        )
    structured["statistics"] = stats.to_dict()
    return ToolResult(text=_summarize(dv, ds, stats.to_dict()), structured=structured)


def _summarize(dataverse: str, dataset: str, stats: dict[str, Any]) -> str:
    """One-line human summary for the content text block."""
    return (
        f"{dataverse}.{dataset}: ~{stats['rowCountEstimate']} rows, "
        f"~{stats['estimatedSizeBytes']} bytes "
        f"(avg {stats['avgItemSizeBytes']} B/doc) from the ANALYZE sample."
    )
