"""check_index_usage: does a query use the indexes available to it?.

Compiles a statement (compile-only) to its optimized logical plan, reuses the
the plan parser to see which datasets it scans, then cross-references each
dataset's secondary indexes from ``Metadata.Index``. Reports indexes the plan
actually uses (``used``) versus indexes that exist but the plan ignores
(``availableButUnused``) — the signal a model needs to recommend a missing
index or rewrite a query that fell back to a full scan.

Defense-in-Depth:
- Layer 1: the schema spells out that this is read-only, compile-only, and must
  be given a complete SELECT.
- Layer 2: pre-flight guards reject an empty statement and known-bad functions
  before any cluster round-trip; a query that does not compile is returned as a
  self-correcting error rather than an empty analysis.
"""

from __future__ import annotations

import json
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError, classify_cc_error
from ..plan_parser import datasets_from_sources, parse_optimized_plan
from ..statement_guard import check_unsupported_functions, strip_set_prefix
from . import ToolResult

_INDEX_QUERY = (
    "SELECT VALUE i FROM Metadata.`Index` i "
    "WHERE i.DataverseName = $dv AND i.DatasetName = $ds AND i.IsPrimary = false;"
)


async def run_check_index_usage(
    client: CCClient,
    settings: Settings,
    *,
    statement: str,
    dataverse: str | None = None,
    user_tag: str | None = None,
) -> ToolResult:
    """Analyze which secondary indexes a query's plan uses vs. ignores."""
    statement = strip_set_prefix(statement)
    if not statement.strip():
        return ToolResult.error(
            GatewayError(ErrorType.INVALID_PARAMETER, "Provide a non-empty SQL++ SELECT statement.")
        )
    bad_function = check_unsupported_functions(statement)
    if bad_function is not None:
        return ToolResult.error(bad_function)

    ccid = make_client_context_id(settings.agent_session_id, user_tag)
    try:
        envelope = await client.compile_query(
            statement, client_context_id=ccid, dataverse=dataverse, emit_plan=True
        )
    except GatewayError as err:
        return ToolResult.error(err)

    error = _classify_envelope_error(envelope)
    if error is not None:
        return ToolResult.error(error)

    parsed = parse_optimized_plan(envelope.get("plans"))
    if parsed is None:
        return ToolResult.error(
            GatewayError(ErrorType.INTERNAL, "AsterixDB returned no optimized plan to analyze.")
        )

    plan_text = json.dumps(envelope.get("plans"), default=str)
    datasets = datasets_from_sources(parsed.data_sources, dataverse)

    used: list[dict[str, Any]] = []
    unused: list[dict[str, Any]] = []
    for dv, ds in datasets:
        for index in await _secondary_indexes(client, ccid, dv, ds):
            record = {"dataverse": dv, "dataset": ds, **index}
            (used if index["index"] in plan_text else unused).append(record)

    has_scan = "data-scan" in parsed.operator_counts
    structured = {
        "status": "success",
        "datasetsAnalyzed": [{"dataverse": dv, "dataset": ds} for dv, ds in datasets],
        "used": used,
        "availableButUnused": unused,
        "usesFullScan": has_scan and not used,
    }
    return ToolResult(text=_summarize(structured), structured=structured)


async def _secondary_indexes(
    client: CCClient, ccid: str, dataverse: str, dataset: str
) -> list[dict[str, Any]]:
    """Fetch a dataset's secondary indexes from the Metadata catalog."""
    try:
        envelope = await client.execute(
            _INDEX_QUERY,
            client_context_id=ccid,
            statement_parameters={"dv": dataverse, "ds": dataset},
        )
    except GatewayError:
        return []
    indexes: list[dict[str, Any]] = []
    for row in envelope.get("results") or []:
        if isinstance(row, dict) and isinstance(row.get("IndexName"), str):
            indexes.append(
                {
                    "index": row["IndexName"],
                    "structure": row.get("IndexStructure"),
                    "searchKey": row.get("SearchKey"),
                }
            )
    return indexes


def _classify_envelope_error(envelope: dict[str, Any]) -> GatewayError | None:
    errors = envelope.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        code = errors[0].get("code")
        message = errors[0].get("msg")
        return classify_cc_error(
            asterix_code=code if isinstance(code, str) else None,
            message=message if isinstance(message, str) else "AsterixDB returned an error.",
        )
    return None


def _summarize(structured: dict[str, Any]) -> str:
    used = len(structured["used"])
    unused = len(structured["availableButUnused"])
    if structured["usesFullScan"]:
        return (
            f"Query uses a full scan; {unused} secondary index(es) available but unused. "
            "Consider an index or a more selective predicate."
        )
    return f"{used} index(es) used, {unused} available but unused."
