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
    "check_index_usage",
    "list_functions",
    "get_function",
    "search_metadata",
    "get_cluster_status",
    "get_node_details",
    "get_reference",
}


def test_registry_covers_exactly_the_tool_surface() -> None:
    assert set(TOOL_ANNOTATIONS) == EXPECTED_TOOLS


def test_no_tool_is_marked_destructive() -> None:
    assert all(a.destructiveHint is False for a in TOOL_ANNOTATIONS.values())


def test_only_cancel_query_is_not_read_only() -> None:
    not_read_only = {n for n, a in TOOL_ANNOTATIONS.items() if a.readOnlyHint is not True}
    assert not_read_only == {"cancel_query"}


def test_only_get_reference_is_closed_world() -> None:
    closed = {n for n, a in TOOL_ANNOTATIONS.items() if a.openWorldHint is False}
    assert closed == {"get_reference"}


def test_only_submit_async_query_is_not_idempotent() -> None:
    not_idem = {n for n, a in TOOL_ANNOTATIONS.items() if a.idempotentHint is not True}
    assert not_idem == {"submit_async_query"}


def test_every_tool_has_a_human_title() -> None:
    assert all(a.title for a in TOOL_ANNOTATIONS.values())
