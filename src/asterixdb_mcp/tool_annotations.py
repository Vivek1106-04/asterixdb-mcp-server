"""Per-tool MCP behavioral hints (``ToolAnnotations``).

High-end MCP clients read these hints to decide whether a tool can be invoked
without an explicit user confirmation. The gateway is read-only by architecture
invariant, so every tool that only reads data carries ``readOnlyHint=True`` and
``destructiveHint=False`` — a client may run them freely during agentic loops.

Hint semantics applied here:

- ``readOnlyHint``  — the tool does not modify data. True for all query,
  discovery, introspection, and reference tools. The single exception is
  ``cancel_query``, which mutates server-side execution state (it aborts a
  running job) without ever touching stored data.
- ``destructiveHint`` — the tool can irreversibly destroy data. False for the
  entire surface; the gateway can never mutate or drop data.
- ``idempotentHint`` — repeating the call adds no further effect. True
  everywhere except ``submit_async_query``, where each call allocates a new
  server-side result handle.
- ``openWorldHint`` — the tool reaches the live external cluster. True for
  everything that calls the Cluster Controller; False only for ``get_reference``,
  which reads static reference material bundled inside the gateway.

These hints are advisory metadata, not an authorization boundary. The
read-only guarantee is enforced independently at egress (``readonly=true`` on
every CC query) — annotations never relax that enforcement.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

# Tools that reach the live cluster and only read data (the common case).
_LIVE_READ_ONLY = {
    "execute_query": "Execute Read-Only SQL++ Query",
    "get_schema": "Get Dataset Schema",
    "list_dataverses": "List Dataverses",
    "list_datasets": "List Datasets",
    "describe_dataverse": "Describe Dataverse",
    "sample_dataset": "Sample Dataset Documents",
    "wait_on_async_query": "Wait on Async Query",
    "fetch_query_result": "Fetch Async Query Result",
    "validate_syntax": "Validate SQL++ Syntax",
    "explain_query": "Explain Query Plan",
    "explain_physical_plan": "Explain Physical Plan",
    "check_index_usage": "Check Index Usage",
    "list_functions": "List SQL++ Functions",
    "get_function": "Get Function Details",
    "search_metadata": "Search Metadata Catalog",
    "get_cluster_status": "Get Cluster Status",
    "get_node_details": "Get Node Details",
    "database_health_check": "Database Health Check",
    "recommend_indexes": "Recommend Indexes",
    "get_dataset_statistics": "Get Dataset Statistics",
    "list_running_queries": "List Running Queries",
    "profile_query": "Profile Query Runtime",
}


def _live_read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )


# name -> ToolAnnotations for every advertised tool.
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    name: _live_read_only(title) for name, title in _LIVE_READ_ONLY.items()
}

# submit_async_query reads data but is NOT idempotent: each call allocates a new
# server-side async result handle.
TOOL_ANNOTATIONS["submit_async_query"] = ToolAnnotations(
    title="Submit Async SQL++ Query",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

# cancel_query mutates server-side execution state (aborts a running job). It is
# the one non-read-only tool, but it never destroys stored data and cancelling an
# already-cancelled job is a no-op, so it stays idempotent and non-destructive.
TOOL_ANNOTATIONS["cancel_query"] = ToolAnnotations(
    title="Cancel Async Query",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

# get_reference reads static documentation bundled in the gateway; it never
# reaches the cluster, so it is a closed-world read.
TOOL_ANNOTATIONS["get_reference"] = ToolAnnotations(
    title="Read SQL++ Reference",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

# get_query_history reads the in-gateway audit log; like get_reference it never
# reaches the cluster, so it is a closed-world read.
TOOL_ANNOTATIONS["get_query_history"] = ToolAnnotations(
    title="Get Query History",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
