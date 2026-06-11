"""Unit tests for the advertised output-schema registry."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from asterixdb_mcp.output_schemas import OUTPUT_SCHEMAS, apply_output_schemas

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
    assert set(OUTPUT_SCHEMAS) == EXPECTED_TOOLS


def test_every_schema_is_a_permissive_object() -> None:
    for name, schema in OUTPUT_SCHEMAS.items():
        assert schema["type"] == "object", name
        # Open for extras so optional fields never fail a client-side check.
        assert schema["additionalProperties"] is True, name
        assert "properties" in schema, name


def test_required_fields_are_declared_properties() -> None:
    for name, schema in OUTPUT_SCHEMAS.items():
        required = set(schema.get("required", []))
        properties = set(schema["properties"])
        assert required <= properties, name


def test_apply_raises_on_unknown_tool() -> None:
    # A FastMCP with no tools registered: every schema name is unknown.
    empty = FastMCP("empty")
    with pytest.raises(KeyError):
        apply_output_schemas(empty)
