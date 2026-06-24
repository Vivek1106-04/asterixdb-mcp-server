"""sample_dataset: a bounded sample of real documents from a dataset.

Declared schema (get_schema) lists field names and types but never the value
domain. A model that filters on a value that does not match how the data is
actually stored (wrong code, casing, format, or unit) silently gets zero rows.
Sampling returns real documents so the model can see actual stored values, and
undeclared fields on OPEN datasets, before it writes a filter.

The statement is built by substituting catalog-verified, backtick-quoted
identifiers into a fixed template, never by formatting SQL around user input.
The only interpolated tokens are validated identifiers and an integer literal.
"""

from __future__ import annotations

from ..artifacts import ArtifactFormat, overflow_artifact_payload
from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..egress import bound_rows_for_llm
from ..errors import ErrorType, GatewayError
from ..inventory import dataset_names, dataverse_names, fetch_dataset_rows
from ..naming import quote_identifier, resolve
from . import ToolResult

DEFAULT_SIZE = 10
MAX_SIZE = 100

_SAMPLE_TEMPLATE = "SELECT VALUE d FROM __TABLE__ AS d LIMIT __N__;"


async def run_sample_dataset(
    client: CCClient,
    settings: Settings,
    *,
    dataverse: str,
    dataset: str,
    size: int = DEFAULT_SIZE,
    download_format: ArtifactFormat | None = None,
) -> ToolResult:
    """Return up to `size` real documents from a dataset, no SQL required."""
    size = min(max(size, 1), MAX_SIZE)
    ccid = make_client_context_id(settings.agent_session_id, "sample_dataset")
    try:
        dv, ds = await _resolve(client, ccid, dataverse, dataset)
        table = quote_identifier(dv) + "." + quote_identifier(ds)
        statement = _SAMPLE_TEMPLATE.replace("__TABLE__", table).replace("__N__", str(size))
        envelope = await client.execute(statement, client_context_id=ccid)
    except GatewayError as err:
        return ToolResult.error(err)

    rows = envelope.get("results") or []
    rows = rows if isinstance(rows, list) else [rows]
    # Egress layer 4: clamp huge field values (e.g. a long review `text`) and cap
    # the payload so a sample can never blow up the client's context window.
    window, egress = bound_rows_for_llm(
        rows, settings.max_rows_to_llm, settings.max_bytes_to_llm, settings.max_field_chars
    )
    # When the sample was trimmed for the context window, save the full set of
    # sampled documents for download instead of dropping the overflow.
    artifact = overflow_artifact_payload(
        rows, overflow=egress["truncated"], settings=settings, fmt=download_format
    )
    if artifact is not None:
        egress["artifact"] = artifact
    structured = {
        "status": "success",
        "dataverse": dv,
        "dataset": ds,
        "sampleSize": size,
        "rowsReturned": len(window),
        "results": window,
        "egress": egress,
    }
    return ToolResult(text=f"Sampled {len(window)} row(s) from {dv}.{ds}.", structured=structured)


async def _resolve(client: CCClient, ccid: str, dataverse: str, dataset: str) -> tuple[str, str]:
    """Resolve (dataverse, dataset) to canonical catalog names or raise NOT_FOUND."""
    rows = await fetch_dataset_rows(client, ccid=ccid)
    dv_canonical, dv_suggestions = resolve(dataverse, dataverse_names(rows))
    if dv_canonical is None:
        raise GatewayError(ErrorType.NOT_FOUND, _miss("Dataverse", dataverse, dv_suggestions))
    ds_canonical, ds_suggestions = resolve(dataset, dataset_names(rows, dv_canonical))
    if ds_canonical is None:
        raise GatewayError(
            ErrorType.NOT_FOUND, _miss(f"Dataset in {dv_canonical}", dataset, ds_suggestions)
        )
    return dv_canonical, ds_canonical


def _miss(what: str, name: str, suggestions: list[str]) -> str:
    base = f"{what} {name!r} was not found."
    if suggestions:
        return base + " Did you mean: " + ", ".join(suggestions) + "?"
    return base
