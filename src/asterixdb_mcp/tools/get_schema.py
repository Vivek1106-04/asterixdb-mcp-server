"""get_schema: declared schema for a single Dataset.

Reads three Metadata collections (Dataset, Datatype, Index) and assembles one
LLM-friendly schema document. The useful bit is datasetFormatInfo, which
surfaces ROW vs COLUMNAR storage so the LLM can avoid SELECT * on columnar
datasets.

The pure transforms (extract_dataset_format_info, extract_record_fields, etc.)
are kept apart from I/O so they unit-test against captured metadata records
without a live cluster.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError
from ..inventory import dataset_names, dataverse_names, fetch_dataset_rows
from ..naming import resolve
from . import ToolResult

# Metadata field names, per MetadataRecordTypes.java. Centralized so a schema
# drift only needs fixing in one place (and the startup self-check guards them).
FIELD_DATASET_FORMAT = "DatasetFormat"
FIELD_FORMAT = "Format"
FORMAT_COLUMNAR_HINT = (
    "Strongly prefer explicit field projection. SELECT * on a COLUMNAR dataset forces "
    "full column-group reconstruction and negates the columnar advantage; such queries "
    "may be rejected at plan analysis (errorType=PLAN_REJECTED)."
)


async def run_get_schema(
    client: CCClient,
    settings: Settings,
    *,
    dataverse: str,
    dataset: str,
) -> ToolResult:
    """Fetch and assemble the schema document for ``dataverse.dataset``."""
    ccid = make_client_context_id(settings.agent_session_id, "get_schema")
    try:
        dataset_record = await _fetch_one(client, ccid, _dataset_query(dataverse, dataset))
        if dataset_record is None:
            hint = await _not_found_hint(client, ccid, dataverse, dataset)
            raise GatewayError(
                ErrorType.NOT_FOUND,
                f"Dataset {dataverse}.{dataset} was not found in Metadata.{hint}",
            )
        datatype_record = await _fetch_one(
            client,
            ccid,
            _datatype_query(
                dataset_record.get("DatatypeDataverseName", dataverse),
                dataset_record.get("DatatypeName", ""),
            ),
        )
        index_records = await _fetch_all(client, ccid, _index_query(dataverse, dataset))
    except GatewayError as err:
        return ToolResult.error(err)

    structured = {
        "status": "success",
        "dataverse": dataverse,
        "dataset": dataset,
        "datatypeName": dataset_record.get("DatatypeName"),
        "primaryKey": extract_primary_key(dataset_record),
        "fields": extract_record_fields(datatype_record or {}),
        "secondaryIndexes": summarize_secondary_indexes(index_records),
        "datasetFormatInfo": extract_dataset_format_info(dataset_record),
    }
    return ToolResult(text=_summarize(structured), structured=structured)


# pure transforms (unit-tested)


def extract_dataset_format_info(dataset_record: dict[str, Any]) -> dict[str, Any]:
    """Map the Metadata DatasetFormat block to a normalized format descriptor.

    AsterixDB stores "row"/"column"; the gateway surfaces ROW/COLUMNAR. Datasets
    that predate the columnar format omit the block and report ROW.
    """
    raw_format = ""
    block = dataset_record.get(FIELD_DATASET_FORMAT)
    if isinstance(block, dict):
        raw_format = str(block.get(FIELD_FORMAT, "")).lower()

    if raw_format == "column":
        return {"format": "COLUMNAR", "projectionHint": FORMAT_COLUMNAR_HINT}
    return {"format": "ROW"}


def extract_primary_key(dataset_record: dict[str, Any]) -> list[str]:
    """Pull the primary-key field names from a Dataset's internal details."""
    details = dataset_record.get("InternalDetails")
    if not isinstance(details, dict):
        return []
    primary_key = details.get("PrimaryKey")
    if not isinstance(primary_key, list):
        return []
    names: list[str] = []
    for entry in primary_key:
        # Each key is itself a list of path segments, e.g. [["id"]] -> "id".
        if isinstance(entry, list):
            names.append(".".join(str(seg) for seg in entry))
        else:
            names.append(str(entry))
    return names


def extract_record_fields(datatype_record: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the top-level field list from a Datatype's derived record type."""
    derived = datatype_record.get("Derived")
    if not isinstance(derived, dict):
        return []
    record = derived.get("Record")
    if not isinstance(record, dict):
        return []
    fields = record.get("Fields")
    if not isinstance(fields, list):
        return []
    extracted: list[dict[str, Any]] = []
    for fld in fields:
        if not isinstance(fld, dict):
            continue
        extracted.append(
            {
                "name": fld.get("FieldName"),
                "type": fld.get("FieldType"),
                "nullable": bool(fld.get("IsNullable", False)),
            }
        )
    return extracted


def summarize_secondary_indexes(index_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten secondary indexes (skip the primary) into a compact list."""
    summary: list[dict[str, Any]] = []
    for idx in index_records:
        if not isinstance(idx, dict) or idx.get("IsPrimary"):
            continue
        summary.append(
            {
                "indexName": idx.get("IndexName"),
                "indexType": idx.get("IndexStructure"),
                "keyFields": idx.get("SearchKey"),
            }
        )
    return summary


# metadata query builders


def _dataset_query(dataverse: str, dataset: str) -> tuple[str, dict[str, Any]]:
    statement = (
        "SELECT VALUE d FROM Metadata.`Dataset` d "
        "WHERE d.DataverseName = $dataverse AND d.DatasetName = $dataset;"
    )
    return statement, {"dataverse": dataverse, "dataset": dataset}


def _datatype_query(datatype_dataverse: str, datatype_name: str) -> tuple[str, dict[str, Any]]:
    statement = (
        "SELECT VALUE t FROM Metadata.`Datatype` t "
        "WHERE t.DataverseName = $dataverse AND t.DatatypeName = $datatype;"
    )
    return statement, {"dataverse": datatype_dataverse, "datatype": datatype_name}


def _index_query(dataverse: str, dataset: str) -> tuple[str, dict[str, Any]]:
    statement = (
        "SELECT VALUE i FROM Metadata.`Index` i "
        "WHERE i.DataverseName = $dataverse AND i.DatasetName = $dataset;"
    )
    return statement, {"dataverse": dataverse, "dataset": dataset}


# I/O helpers


async def _fetch_all(
    client: CCClient, ccid: str, query: tuple[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    statement, params = query
    envelope = await client.execute(statement, client_context_id=ccid, statement_parameters=params)
    rows = envelope.get("results") or []
    return [r for r in rows if isinstance(r, dict)]


async def _fetch_one(
    client: CCClient, ccid: str, query: tuple[str, dict[str, Any]]
) -> dict[str, Any] | None:
    rows = await _fetch_all(client, ccid, query)
    return rows[0] if rows else None


async def _not_found_hint(client: CCClient, ccid: str, dataverse: str, dataset: str) -> str:
    """Build a 'did you mean' suffix by matching the missed name against the catalog."""
    rows = await fetch_dataset_rows(client, ccid=ccid)
    dv_canonical, dv_suggestions = resolve(dataverse, dataverse_names(rows))
    if dv_canonical is None:
        if dv_suggestions:
            return " Did you mean dataverse: " + ", ".join(dv_suggestions) + "?"
        return ""
    _, ds_suggestions = resolve(dataset, dataset_names(rows, dv_canonical))
    if ds_suggestions:
        return " Did you mean: " + ", ".join(ds_suggestions) + "?"
    return ""


def _summarize(structured: dict[str, Any]) -> str:
    fmt = structured["datasetFormatInfo"]["format"]
    n_fields = len(structured["fields"])
    n_idx = len(structured["secondaryIndexes"])
    return (
        f"Schema for {structured['dataverse']}.{structured['dataset']}: "
        f"{n_fields} declared field(s), {n_idx} secondary index(es), storage format {fmt}."
    )
