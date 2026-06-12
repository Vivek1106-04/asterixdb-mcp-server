"""Parameterized resource templates for dataset- and dataverse-scoped context.

Static resources expose fixed URIs; a template exposes a URI *pattern* whose
variables a client fills in (``asterixdb://schema/{dataverse}/{dataset}``). This
lets a high-end client pin "this dataset's schema" as attached context without
spending a tool call, and the template variables autocomplete through the same
``completion/complete`` handler that serves prompt arguments.

Each reader delegates to the corresponding read-only tool core, so name
validation, case-insensitive resolution, columnar awareness, and egress controls
are applied exactly as they are for the equivalent tool — there is no second code
path and no new injection surface (the URI variables are passed as identifiers to
the same guarded cores, never spliced into SQL here). A read returns the tool's
structured envelope as JSON: the success payload, or the gateway error envelope
when resolution fails, so a bad name yields an informative document rather than a
protocol-level failure.
"""

from __future__ import annotations

import json

from ..cc_client import CCClient
from ..config import Settings
from ..tools.describe_dataverse import run_describe_dataverse
from ..tools.get_schema import run_get_schema
from ..tools.list_datasets import run_list_datasets
from ..tools.sample_dataset import run_sample_dataset

# A resource read cannot take parameters, so the per-dataset sample uses a small
# fixed window — enough to reveal real value shapes without a large transfer.
_SAMPLE_SIZE = 10


async def read_dataset_schema(
    client: CCClient, settings: Settings, *, dataverse: str, dataset: str
) -> str:
    """asterixdb://schema/{dataverse}/{dataset} — one dataset's declared schema."""
    result = await run_get_schema(client, settings, dataverse=dataverse, dataset=dataset)
    return json.dumps(result.structured, default=str)


async def read_dataverse_schema(client: CCClient, settings: Settings, *, dataverse: str) -> str:
    """asterixdb://dataverse/{dataverse} — full schema of every dataset in a dataverse."""
    result = await run_describe_dataverse(client, settings, dataverse=dataverse)
    return json.dumps(result.structured, default=str)


async def read_dataset_sample(
    client: CCClient, settings: Settings, *, dataverse: str, dataset: str
) -> str:
    """asterixdb://sample/{dataverse}/{dataset} — a small sample of real documents."""
    result = await run_sample_dataset(
        client, settings, dataverse=dataverse, dataset=dataset, size=_SAMPLE_SIZE
    )
    return json.dumps(result.structured, default=str)


async def read_dataverse_datasets(client: CCClient, settings: Settings, *, dataverse: str) -> str:
    """asterixdb://datasets/{dataverse} — dataset summaries within one dataverse."""
    result = await run_list_datasets(client, settings, dataverse=dataverse)
    return json.dumps(result.structured, default=str)
