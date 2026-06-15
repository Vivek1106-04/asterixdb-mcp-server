"""analyze_dataverse: bootstrap a Dataverse exploration session.

Injects the dataset inventory, the storage-format awareness rule, and the
safety contract into the agent's context. When the Dataverse has more than
DATASET_SELECTION_THRESHOLD datasets, the prompt requires a specific dataset
argument so it can embed one full schema instead of flooding the context window
with every schema in the Dataverse.
"""

from __future__ import annotations

import json
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..tools.get_schema import run_get_schema
from ..tools.list_datasets import run_list_datasets
from . import STORAGE_FORMAT_AWARENESS_BLOCK

# Above this dataset count, the prompt asks the agent to name a single dataset.
DATASET_SELECTION_THRESHOLD = 10


async def run_analyze_dataverse(
    client: CCClient,
    settings: Settings,
    *,
    dataverse: str | None = None,
    dataset: str | None = None,
) -> str:
    """Compose the analyze_dataverse prompt text for ``dataverse``.

    ``dataverse`` is optional so the prompt is usable in clients that invoke it
    without first collecting arguments; when omitted the prompt returns guidance
    on how to choose one rather than failing.
    """
    if not dataverse:
        return _needs_dataverse_text()
    listing = await run_list_datasets(client, settings, dataverse=dataverse)
    if listing.is_error:
        return _error_text(dataverse, listing.structured)

    names = [d.get("dataset") for d in listing.structured.get("datasets", [])]
    total = listing.structured.get("totalDatasets", len(names))

    if total > DATASET_SELECTION_THRESHOLD and not dataset:
        return compose_analyze_dataverse(
            dataverse, names, total=total, needs_dataset_selection=True
        )

    schema: dict[str, Any] | None = None
    if dataset:
        schema_result = await run_get_schema(client, settings, dataverse=dataverse, dataset=dataset)
        schema = (
            schema_result.structured
            if not schema_result.is_error
            else {"error": schema_result.structured}
        )

    return compose_analyze_dataverse(dataverse, names, total=total, schema=schema)


def compose_analyze_dataverse(
    dataverse: str,
    dataset_names: list[str | None],
    *,
    total: int,
    schema: dict[str, Any] | None = None,
    needs_dataset_selection: bool = False,
) -> str:
    """Pure template assembly: no I/O, directly unit-testable."""
    lines = [
        f"# Exploring Dataverse `{dataverse}`",
        "",
        f"This Dataverse contains {total} dataset(s):",
        _format_name_list(dataset_names),
        "",
        STORAGE_FORMAT_AWARENESS_BLOCK,
        "",
        "## Safety Contract",
        "- All queries are READ-ONLY; mutations are rejected by the database.",
        "- Always include a LIMIT clause (start with LIMIT 20).",
        "- Call get_schema before composing a query so you respect the storage format.",
        "- Use validate_syntax for unfamiliar queries before spending an execution slot.",
    ]

    if needs_dataset_selection:
        lines += [
            "",
            "## Next Step",
            f"This Dataverse has more than {DATASET_SELECTION_THRESHOLD} datasets. "
            "Re-invoke this prompt with a specific `dataset` argument to embed its full "
            "schema, or call get_schema directly on the dataset you care about.",
        ]
    elif schema is not None:
        lines += [
            "",
            "## Embedded Schema",
            "```json",
            json.dumps(schema, indent=2, default=str),
            "```",
        ]

    return "\n".join(lines)


def _format_name_list(names: list[str | None]) -> str:
    rendered = [f"- `{n}`" for n in names if n]
    return "\n".join(rendered) if rendered else "- (none)"


def _needs_dataverse_text() -> str:
    return (
        "# Explore a Dataverse\n\n"
        "No `dataverse` was provided. Call list_dataverses to see what exists, then "
        "re-invoke analyze_dataverse with a `dataverse` argument (and optionally a "
        "`dataset`) to embed its inventory and schema."
    )


def _error_text(dataverse: str, structured: dict[str, Any]) -> str:
    message = structured.get("errorMessage", "unknown error")
    return (
        f"# Exploring Dataverse `{dataverse}`\n\n"
        f"Could not list datasets: {message}\n\n"
        "Verify the Dataverse name and that the cluster is reachable "
        "(read asterixdb://cluster/status)."
    )
