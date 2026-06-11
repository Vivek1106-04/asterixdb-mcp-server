"""Advertised ``outputSchema`` for each tool's successful structured result.

High-end MCP clients read a tool's ``outputSchema`` to anticipate the shape of
its result before calling it, and to plan multi-step chains (e.g. that
``submit_async_query`` yields a ``clientContextID`` that ``fetch_query_result``
consumes). Declaring the success shape makes those data dependencies explicit.

These schemas describe the SUCCESSFUL result envelope only. Per the MCP
specification, ``outputSchema`` characterizes successful results; an error
response is flagged with ``isError`` and carries the gateway error envelope
instead, so it is exempt from the success schema. Every schema therefore sets
``additionalProperties: true`` and keeps ``required`` to the fields that are
always present on success — optional fields (metrics, paging, profile data)
never make a valid result fail a client-side check.

Advertisement is intentionally decoupled from runtime validation: these schemas
are attached only to the value a client sees in ``tools/list`` (see
``apply_output_schemas``); the gateway never validates its own heterogeneous
structured content against them, which would otherwise reject the error
envelope. Enforcement of the read-only and egress guarantees lives elsewhere and
is unaffected by this metadata.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

_JSON = dict[str, Any]


def _obj(properties: _JSON, *, required: tuple[str, ...] = (), description: str = "") -> _JSON:
    """Build a permissive object schema: documented properties, open for extras."""
    schema: _JSON = {
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


_STRING: _JSON = {"type": "string"}
_INT: _JSON = {"type": "integer"}
_BOOL: _JSON = {"type": "boolean"}
_OBJECT: _JSON = {"type": "object", "additionalProperties": True}


def _array_of_objects() -> _JSON:
    return {"type": "array", "items": {"type": "object", "additionalProperties": True}}


# name -> JSON Schema for the successful structured result.
OUTPUT_SCHEMAS: dict[str, _JSON] = {
    "execute_query": _obj(
        {
            "status": _STRING,
            "results": _array_of_objects(),
            "rowsReturned": _INT,
            "rowsAvailableInResponse": _INT,
            "moreAvailable": _BOOL,
            "offset": _INT,
            "limit": _INT,
            "clientContextID": _STRING,
            "egress": _OBJECT,
            "metrics": _OBJECT,
        },
        required=("status", "results"),
        description="Rows from a synchronous read-only query, with paging and egress metadata.",
    ),
    "get_schema": _obj(
        {
            "status": _STRING,
            "dataverse": _STRING,
            "dataset": _STRING,
            "datatypeName": _STRING,
            "primaryKey": {"type": "array"},
            "fields": _array_of_objects(),
            "secondaryIndexes": _array_of_objects(),
            "datasetFormatInfo": _obj({"format": _STRING}),
        },
        required=("dataset", "fields", "datasetFormatInfo"),
        description="Declared schema of one dataset, including ROW vs COLUMNAR storage format.",
    ),
    "list_dataverses": _obj(
        {
            "status": _STRING,
            "count": _INT,
            "dataverses": _array_of_objects(),
        },
        required=("dataverses",),
        description="Every dataverse (namespace) on the cluster.",
    ),
    "list_datasets": _obj(
        {
            "status": _STRING,
            "dataverseFilter": {"type": ["string", "null"]},
            "totalDatasets": _INT,
            "offset": _INT,
            "limit": _INT,
            "moreAvailable": _BOOL,
            "datasets": _array_of_objects(),
            "nameCollisions": {"type": "array"},
        },
        required=("datasets",),
        description="Paginated dataset summaries, optionally scoped to one dataverse.",
    ),
    "describe_dataverse": _obj(
        {
            "status": _STRING,
            "dataverse": _STRING,
            "datasetCount": _INT,
            "describedCount": _INT,
            "truncated": _BOOL,
            "datasets": _array_of_objects(),
        },
        required=("dataverse", "datasets"),
        description="Full schema of every dataset in one dataverse.",
    ),
    "sample_dataset": _obj(
        {
            "status": _STRING,
            "dataverse": _STRING,
            "dataset": _STRING,
            "sampleSize": _INT,
            "rowsReturned": _INT,
            "results": _array_of_objects(),
            "egress": _OBJECT,
        },
        required=("results",),
        description="A small bounded sample of real documents from a dataset.",
    ),
    "submit_async_query": _obj(
        {
            "status": _STRING,
            "clientContextID": _STRING,
            "queryStatus": _STRING,
            "done": _BOOL,
        },
        required=("clientContextID",),
        description="Handle for an async query; the clientContextID drives the whole lifecycle.",
    ),
    "wait_on_async_query": _obj(
        {
            "status": _STRING,
            "clientContextID": _STRING,
            "done": _BOOL,
            "failed": _BOOL,
            "timeout": _BOOL,
            "cancelled": _BOOL,
            "fatal": _BOOL,
            "queryStatus": _STRING,
        },
        required=("clientContextID", "done"),
        description="Completion state of an async query; done:true means results are ready.",
    ),
    "fetch_query_result": _obj(
        {
            "status": _STRING,
            "clientContextID": _STRING,
            "results": _array_of_objects(),
            "rowsReturned": _INT,
            "rowsAvailableInResponse": _INT,
            "moreAvailable": _BOOL,
            "offset": _INT,
            "limit": _INT,
            "egress": _OBJECT,
        },
        required=("clientContextID", "results"),
        description="A page of rows from a completed async query.",
    ),
    "cancel_query": _obj(
        {
            "status": _STRING,
            "clientContextID": _STRING,
            "cancelled": _BOOL,
        },
        required=("clientContextID",),
        description="Result of cancelling an in-flight async query.",
    ),
    "validate_syntax": _obj(
        {
            "status": _STRING,
            "valid": _BOOL,
            "errorType": _STRING,
            "errorMessage": _STRING,
        },
        required=("valid",),
        description="Whether a statement compiles; valid:false distinguishes syntax vs semantics.",
    ),
    "explain_query": _obj(
        {
            "status": _STRING,
            "valid": _BOOL,
            "plan": _OBJECT,
        },
        required=("plan",),
        description="The optimized logical plan as a structured operator tree.",
    ),
    "check_index_usage": _obj(
        {
            "status": _STRING,
            "datasetsAnalyzed": _INT,
            "used": _array_of_objects(),
            "availableButUnused": _array_of_objects(),
            "usesFullScan": _BOOL,
        },
        required=("used", "availableButUnused", "usesFullScan"),
        description="Which secondary indexes a query's plan uses, ignores, or scans past.",
    ),
    "list_functions": _obj(
        {
            "status": _STRING,
            "total": _INT,
            "offset": _INT,
            "limit": _INT,
            "moreAvailable": _BOOL,
            "functions": _array_of_objects(),
        },
        required=("functions",),
        description="Built-in and user-defined SQL++ functions, paginated and filterable.",
    ),
    "get_function": _obj(
        {
            "status": _STRING,
            "name": _STRING,
            "dataverse": _STRING,
            "category": _STRING,
            "summary": _STRING,
            "language": _STRING,
            "params": {"type": "array"},
            "returnType": _STRING,
            "definition": _STRING,
            "arity": _INT,
            "scope": _STRING,
        },
        required=("name",),
        description="One function's signature and details (built-in summary or UDF body).",
    ),
    "search_metadata": _obj(
        {
            "status": _STRING,
            "query": _STRING,
            "totalMatches": _INT,
            "limit": _INT,
            "matches": _array_of_objects(),
        },
        required=("matches",),
        description="Fuzzy name matches across datasets, types, indexes, functions, and feeds.",
    ),
    "get_cluster_status": _obj(
        {
            "status": _STRING,
            "version": _STRING,
            "state": _STRING,
            "nodeCount": _INT,
            "nodes": _array_of_objects(),
        },
        required=("nodes",),
        description="Cluster version, overall state, and the per-node roster.",
    ),
    "get_node_details": _obj(
        {
            "status": _STRING,
            "node": _STRING,
            "details": _OBJECT,
        },
        required=("details",),
        description="Per-node-controller statistics for one node id.",
    ),
    "get_reference": _obj(
        {
            "status": _STRING,
            "topic": _STRING,
            "topics": _OBJECT,
            "all": _BOOL,
        },
        description="Curated in-gateway SQL++ reference content for one topic or all topics.",
    ),
}


def apply_output_schemas(mcp: FastMCP) -> None:
    """Attach each advertised output schema to its registered tool.

    The schema is seeded into the ``Tool.output_schema`` cached value so it is
    reported in ``tools/list``, while ``fn_metadata.output_schema`` is left unset
    so the SDK never validates the gateway's structured content against it (which
    would reject the error envelope). Raises ``KeyError`` if a schema names a tool
    that is not registered, catching drift in tests.
    """
    manager = mcp._tool_manager
    for name, schema in OUTPUT_SCHEMAS.items():
        tool = manager.get_tool(name)
        if tool is None:
            raise KeyError(f"output schema names unknown tool: {name}")
        # Seed the cached_property value without enabling runtime validation.
        tool.__dict__["output_schema"] = schema
