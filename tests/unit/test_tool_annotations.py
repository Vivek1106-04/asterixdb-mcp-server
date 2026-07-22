"""Unit tests for the per-tool annotation registry."""

from __future__ import annotations

from asterixdb_mcp.tool_annotations import TOOL_ANNOTATIONS

EXPECTED_TOOLS = {
    "execute_query",
    "get_schema",
    "list_dataverses",
    "list_datasets",
    "describe_dataverse",
    "sample_dataset",
    "submit_async_query",
    "wait_on_async_query",
    "fetch_query_result",
    "cancel_query",
    "validate_syntax",
    "explain_query",
    "explain_physical_plan",
    "check_index_usage",
    "list_functions",
    "get_function",
    "memory_search",
    "memory_write",
    "remember_preference",
    "search_metadata",
    "get_cluster_status",
    "get_node_details",
    "get_reference",
    "database_health_check",
    "get_query_history",
    "recommend_indexes",
    "get_dataset_statistics",
    "list_running_queries",
    "profile_query",
}


def test_registry_covers_exactly_the_tool_surface() -> None:
    assert set(TOOL_ANNOTATIONS) == EXPECTED_TOOLS


def test_no_tool_is_marked_destructive() -> None:
    assert all(a.destructiveHint is False for a in TOOL_ANNOTATIONS.values())


def test_only_sanctioned_tools_are_not_read_only() -> None:
    # cancel_query mutates execution state; memory_write and remember_preference
    # persist into the AgentMemory.Memory store (settings-gated, scoped in the CC
    # client). Every other tool stays readonly=true end to end.
    not_read_only = {n for n, a in TOOL_ANNOTATIONS.items() if a.readOnlyHint is not True}
    assert not_read_only == {"cancel_query", "memory_write", "remember_preference"}


def test_only_in_gateway_reads_are_closed_world() -> None:
    # Closed-world tools never reach the cluster: static reference docs and the
    # in-gateway query-history log.
    closed = {n for n, a in TOOL_ANNOTATIONS.items() if a.openWorldHint is False}
    assert closed == {"get_reference", "get_query_history"}


def test_only_submit_async_query_is_not_idempotent() -> None:
    not_idem = {n for n, a in TOOL_ANNOTATIONS.items() if a.idempotentHint is not True}
    assert not_idem == {"submit_async_query"}


def test_every_tool_has_a_human_title() -> None:
    assert all(a.title for a in TOOL_ANNOTATIONS.values())
