"""describe_dataverse: one-shot inventory and schema overview for a dataverse.

A small model otherwise discovers a dataverse across many turns and loses track.
This tool resolves the dataverse and embeds every dataset's schema in a single
response, so the model can explain the whole dataverse from one tool call.

It batches the Metadata reads: one query each for the dataverse's Dataset,
Datatype, and Index records, then joins them in memory. Cost is three queries
regardless of how many datasets the dataverse has, not three-per-dataset.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError
from ..inventory import dataverse_names, fetch_dataset_rows
from ..naming import resolve
from . import ToolResult
from .get_schema import (
    extract_dataset_format_info,
    extract_primary_key,
    extract_record_fields,
    summarize_secondary_indexes,
)

# Embedded-schema cap purely bounds response size; query cost is fixed at three
# regardless. Sized so ordinary dataverses are never truncated.
MAX_DESCRIBE = 50

_DATATYPE_QUERY = "SELECT VALUE t FROM Metadata.`Datatype` t WHERE t.DataverseName = $dv;"
_INDEX_QUERY = "SELECT VALUE i FROM Metadata.`Index` i WHERE i.DataverseName = $dv;"


async def run_describe_dataverse(
    client: CCClient, settings: Settings, *, dataverse: str
) -> ToolResult:
    """Resolve a dataverse and return every dataset's schema from three queries."""
    ccid = make_client_context_id(settings.agent_session_id, "describe_dataverse")
    try:
        all_rows = await fetch_dataset_rows(client, ccid=ccid)
        canonical, suggestions = resolve(dataverse, dataverse_names(all_rows))
        if canonical is None:
            hint = " Did you mean: " + ", ".join(suggestions) + "?" if suggestions else ""
            raise GatewayError(ErrorType.NOT_FOUND, f"Dataverse {dataverse!r} was not found.{hint}")
        datatypes = await _fetch_scoped(client, ccid, _DATATYPE_QUERY, canonical)
        indexes = await _fetch_scoped(client, ccid, _INDEX_QUERY, canonical)
    except GatewayError as err:
        return ToolResult.error(err)

    dataset_records = [r for r in all_rows if r.get("DataverseName") == canonical]
    datatype_by_name = {
        t["DatatypeName"]: t for t in datatypes if isinstance(t.get("DatatypeName"), str)
    }
    indexes_by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index in indexes:
        owner = index.get("DatasetName")
        if isinstance(owner, str):
            indexes_by_dataset[owner].append(index)

    described = [
        _assemble(canonical, record, datatype_by_name, indexes_by_dataset)
        for record in dataset_records[:MAX_DESCRIBE]
    ]
    structured = {
        "status": "success",
        "dataverse": canonical,
        "datasetCount": len(dataset_records),
        "describedCount": len(described),
        "truncated": len(dataset_records) > MAX_DESCRIBE,
        "datasets": described,
    }
    return ToolResult(text=_summarize(structured), structured=structured)


async def _fetch_scoped(
    client: CCClient, ccid: str, statement: str, dataverse: str
) -> list[dict[str, Any]]:
    envelope = await client.execute(
        statement, client_context_id=ccid, statement_parameters={"dv": dataverse}
    )
    rows = envelope.get("results") or []
    return [r for r in rows if isinstance(r, dict)]


def _assemble(
    dataverse: str,
    dataset_record: dict[str, Any],
    datatype_by_name: dict[str, dict[str, Any]],
    indexes_by_dataset: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    datatype_name = dataset_record.get("DatatypeName")
    datatype_record = datatype_by_name.get(datatype_name, {}) if datatype_name else {}
    dataset_name = dataset_record.get("DatasetName")
    secondary = indexes_by_dataset.get(dataset_name, []) if isinstance(dataset_name, str) else []
    return {
        "status": "success",
        "dataverse": dataverse,
        "dataset": dataset_name,
        "datatypeName": datatype_name,
        "primaryKey": extract_primary_key(dataset_record),
        "fields": extract_record_fields(datatype_record),
        "secondaryIndexes": summarize_secondary_indexes(secondary),
        "datasetFormatInfo": extract_dataset_format_info(dataset_record),
    }


# Cap on per-dataset lines spelled out in the text block; the full per-dataset
# schema is always in structuredContent.datasets.
_MAX_LINES_IN_TEXT = 20


def _summarize(structured: dict[str, Any]) -> str:
    note = " (truncated)" if structured["truncated"] else ""
    head = (
        f"Dataverse {structured['dataverse']}: described "
        f"{structured['describedCount']} of {structured['datasetCount']} dataset(s){note}."
    )
    # A resolved dataverse always has at least one dataset, so the list is never
    # empty here.
    datasets = structured["datasets"]
    lines = [_dataset_line(d) for d in datasets[:_MAX_LINES_IN_TEXT]]
    remaining = len(datasets) - _MAX_LINES_IN_TEXT
    if remaining > 0:
        lines.append(f"  …and {remaining} more (see structuredContent).")
    return head + "\n" + "\n".join(lines)


def _dataset_line(dataset: dict[str, Any]) -> str:
    """One compact line per dataset: name, primary key, index count, storage format."""
    primary_key = ", ".join(dataset.get("primaryKey") or []) or "none"
    secondary = len(dataset.get("secondaryIndexes") or [])
    fmt = dataset.get("datasetFormatInfo", {}).get("format", "?")
    return (
        f"  - {dataset.get('dataset')}: PK [{primary_key}]; "
        f"{secondary} secondary index(es); {fmt}"
    )
