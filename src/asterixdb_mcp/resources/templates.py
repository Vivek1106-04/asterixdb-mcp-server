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
from ..context_id import make_client_context_id
from ..index_catalog import fetch_indexes_detailed
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


async def read_dataset_indexes(
    client: CCClient, settings: Settings, *, dataverse: str, dataset: str
) -> str:
    """asterixdb://indexes/{dataverse}/{dataset} — detailed secondary indexes on one dataset."""
    ccid = make_client_context_id(settings.agent_session_id, "indexes_resource")
    indexes = await fetch_indexes_detailed(client, ccid, dataverse=dataverse, dataset=dataset)
    envelope = {
        "status": "success",
        "dataverse": dataverse,
        "dataset": dataset,
        "indexCount": len(indexes),
        "indexes": [index.to_dict() for index in indexes],
    }
    return json.dumps(envelope, default=str)


async def read_dataverse_indexes(client: CCClient, settings: Settings, *, dataverse: str) -> str:
    """asterixdb://indexes/{dataverse} — detailed secondary index inventory for a dataverse."""
    ccid = make_client_context_id(settings.agent_session_id, "indexes_resource")
    indexes = await fetch_indexes_detailed(client, ccid, dataverse=dataverse)
    envelope = {
        "status": "success",
        "dataverse": dataverse,
        "indexCount": len(indexes),
        "indexes": [index.to_dict() for index in indexes],
    }
    return json.dumps(envelope, default=str)
