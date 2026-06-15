"""list_datasets: paginated lightweight Dataset listing.

The discovery primitive: without it the LLM has no Dataset names to point
get_schema at. Returns a cheap summary per dataset (name, datatype, type,
storage format) and pages with offset/limit so a Dataverse with hundreds of
datasets cannot blow up the context window.

When the same bare dataset name exists in more than one dataverse, those entries
are flagged (`nameCollision`) and probed for emptiness (`isEmpty`) so a model can
tell apart same-named datasets and pick the populated one instead of guessing.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import GatewayError
from ..naming import quote_identifier
from . import ToolResult
from .get_schema import extract_dataset_format_info

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
MAX_EMPTY_PROBES = 20

_EMPTY_PROBE_TEMPLATE = "SELECT VALUE 1 FROM __TABLE__ LIMIT 1;"


async def run_list_datasets(
    client: CCClient,
    settings: Settings,
    *,
    dataverse: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> ToolResult:
    """List datasets, optionally scoped to a single Dataverse, with pagination."""
    offset = max(offset, 0)
    limit = min(max(limit, 1), MAX_LIMIT)
    ccid = make_client_context_id(settings.agent_session_id, "list_datasets")

    statement, params = _query(dataverse)
    try:
        envelope = await client.execute(
            statement, client_context_id=ccid, statement_parameters=params
        )
    except GatewayError as err:
        return ToolResult.error(err)

    rows = [r for r in (envelope.get("results") or []) if isinstance(r, dict)]
    window = rows[offset : offset + limit]
    datasets = [_summarize_dataset(r) for r in window]
    await _annotate_collisions(client, ccid, rows, datasets)

    structured = {
        "status": "success",
        "dataverseFilter": dataverse,
        "totalDatasets": len(rows),
        "offset": offset,
        "limit": limit,
        "moreAvailable": offset + limit < len(rows),
        "nameCollisions": sum(1 for d in datasets if d.get("nameCollision")),
        "datasets": datasets,
    }
    return ToolResult(text=_summarize(structured), structured=structured)


def _summarize_dataset(record: dict[str, Any]) -> dict[str, Any]:
    """Compact per-dataset summary line."""
    return {
        "dataverse": record.get("DataverseName"),
        "dataset": record.get("DatasetName"),
        "datatypeName": record.get("DatatypeName"),
        "datasetType": record.get("DatasetType"),
        "format": extract_dataset_format_info(record)["format"],
    }


async def _annotate_collisions(
    client: CCClient, ccid: str, all_rows: list[dict[str, Any]], datasets: list[dict[str, Any]]
) -> None:
    """Flag datasets whose bare name spans multiple dataverses and probe emptiness.

    A name is a collision when it appears in more than one dataverse (dataset
    names are unique within a dataverse). Colliding entries are probed with a
    cheap LIMIT 1 so the model can prefer the populated one.
    """
    counts: dict[str, int] = {}
    for row in all_rows:
        name = row.get("DatasetName")
        if isinstance(name, str):
            counts[name] = counts.get(name, 0) + 1

    probes = 0
    for summary in datasets:
        name = summary["dataset"]
        if not isinstance(name, str) or counts.get(name, 0) <= 1:
            continue
        summary["nameCollision"] = True
        if probes >= MAX_EMPTY_PROBES:
            continue
        probes += 1
        empty = await _probe_empty(client, ccid, summary["dataverse"], name)
        if empty is not None:
            summary["isEmpty"] = empty


async def _probe_empty(client: CCClient, ccid: str, dataverse: Any, dataset: str) -> bool | None:
    """Return True/False for emptiness, or None if the dataset cannot be probed."""
    if not isinstance(dataverse, str):
        return None
    try:
        table = quote_identifier(dataverse) + "." + quote_identifier(dataset)
        envelope = await client.execute(
            _EMPTY_PROBE_TEMPLATE.replace("__TABLE__", table), client_context_id=ccid
        )
    except GatewayError:
        return None
    return len(envelope.get("results") or []) == 0


def _query(dataverse: str | None) -> tuple[str, dict[str, Any]]:
    base = "SELECT VALUE d FROM Metadata.`Dataset` d"
    order = " ORDER BY d.DataverseName, d.DatasetName;"
    if dataverse:
        return base + " WHERE d.DataverseName = $dataverse" + order, {"dataverse": dataverse}
    return base + order, {}


# Cap on how many names are spelled out in the human-readable text so a wide page
# (limit up to 500) cannot bloat the content block; the full list is always in
# structuredContent.datasets.
_MAX_NAMES_IN_TEXT = 30


def _summarize(structured: dict[str, Any]) -> str:
    scope = (
        f"in Dataverse {structured['dataverseFilter']}"
        if structured["dataverseFilter"]
        else "across all Dataverses"
    )
    shown = len(structured["datasets"])
    more = " (more available, page with offset)" if structured["moreAvailable"] else ""
    collision = (
        f" {structured['nameCollisions']} name(s) span multiple dataverses — check isEmpty."
        if structured["nameCollisions"]
        else ""
    )
    head = f"{shown} of {structured['totalDatasets']} dataset(s) {scope}"
    if shown == 0:
        return f"{head}{more}.{collision}"
    return f"{head}{more}: {_name_list(structured)}.{collision}"


def _name_list(structured: dict[str, Any]) -> str:
    """Comma-joined dataset names for the text block, bounded and qualified.

    Names are qualified as ``dataverse.dataset`` only when the listing spans all
    dataverses (no filter), so a scoped listing stays terse. Beyond the cap, the
    remainder is summarized as ``+N more`` (full list is in structuredContent).
    """
    qualify = structured["dataverseFilter"] is None
    datasets = structured["datasets"]
    names: list[str] = []
    for summary in datasets[:_MAX_NAMES_IN_TEXT]:
        name = str(summary.get("dataset"))
        dataverse = summary.get("dataverse")
        names.append(f"{dataverse}.{name}" if qualify and dataverse else name)
    joined = ", ".join(names)
    remaining = len(datasets) - _MAX_NAMES_IN_TEXT
    return f"{joined}, +{remaining} more" if remaining > 0 else joined
