"""Per-tool MCP behavioral hints (``ToolAnnotations``).

High-end MCP clients read these hints to decide whether a tool can be invoked
without an explicit user confirmation. The gateway is read-only by architecture
invariant, so every tool that only reads data carries ``read_only_hint=True`` and
``destructive_hint=False`` — a client may run them freely during agentic loops.

Field names below are the Python spelling. On the wire they stay camelCase
(``readOnlyHint`` and friends): the SDK carries an alias per field, so this
module's keyword arguments can be renamed without changing what a client sees.

Hint semantics applied here:

- ``read_only_hint``  — the tool does not modify data. True for all query,
  discovery, introspection, and reference tools. The single exception is
  ``cancel_query``, which mutates server-side execution state (it aborts a
  running job) without ever touching stored data.
- ``destructive_hint`` — the tool can irreversibly destroy data. False for the
  entire surface; the gateway can never mutate or drop data.
- ``idempotent_hint`` — repeating the call adds no further effect. True
  everywhere except ``submit_async_query``, where each call allocates a new
  server-side result handle.
- ``open_world_hint`` — the tool reaches the live external cluster. True for
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
    "memory_search": "Search Agent Memory",
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
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )


# name -> ToolAnnotations for every advertised tool.
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    name: _live_read_only(title) for name, title in _LIVE_READ_ONLY.items()
}

# submit_async_query reads data but is NOT idempotent: each call allocates a new
# server-side async result handle.
TOOL_ANNOTATIONS["submit_async_query"] = ToolAnnotations(
    title="Submit Async SQL++ Query",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

# cancel_query mutates server-side execution state (aborts a running job). It is
# the one non-read-only tool, but it never destroys stored data and cancelling an
# already-cancelled job is a no-op, so it stays idempotent and non-destructive.
TOOL_ANNOTATIONS["cancel_query"] = ToolAnnotations(
    title="Cancel Async Query",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

# memory_write persists agent-curated notes into the AgentMemory.Memory store —
# the one write surface, gated by settings and scoped to that store in the CC
# client. It supersedes rather than deletes, and re-writing the same note is a
# no-op, so it is non-destructive and idempotent.
TOOL_ANNOTATIONS["memory_write"] = ToolAnnotations(
    title="Write Agent Memory",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

# remember_preference persists a query-writing rule into AgentMemory.Memory,
# same gated write surface as memory_write. Re-recording an identical rule is a
# no-op, so it is non-destructive and idempotent.
TOOL_ANNOTATIONS["remember_preference"] = ToolAnnotations(
    title="Remember Query Preference",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

# get_reference reads static documentation bundled in the gateway; it never
# reaches the cluster, so it is a closed-world read.
TOOL_ANNOTATIONS["get_reference"] = ToolAnnotations(
    title="Read SQL++ Reference",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# get_query_history reads the in-gateway audit log; like get_reference it never
# reaches the cluster, so it is a closed-world read.
TOOL_ANNOTATIONS["get_query_history"] = ToolAnnotations(
    title="Get Query History",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
