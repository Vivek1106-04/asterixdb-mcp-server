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
    "explain_physical_plan",
    "check_index_usage",
    "list_functions",
    "get_function",
    "search_metadata",
    "get_cluster_status",
    "get_node_details",
    "get_reference",
    "database_health_check",
    "get_query_history",
    "recommend_indexes",
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


# Tools whose ``results`` hold query rows: SQL++ SELECT VALUE can return scalars
# or arrays, so the row shape must stay unconstrained.
_RESULT_ROW_TOOLS = ("execute_query", "sample_dataset", "fetch_query_result")


def test_result_carrying_schemas_do_not_constrain_row_shape() -> None:
    for name in _RESULT_ROW_TOOLS:
        results_schema = OUTPUT_SCHEMAS[name]["properties"]["results"]
        assert results_schema == {"type": "array"}, name


def test_execute_query_schema_accepts_scalar_and_object_rows() -> None:
    # Mirrors the client-side check (mcp.client.session validates structured
    # content with jsonschema). A SELECT VALUE COUNT(*) row [46219] must pass.
    from jsonschema import validate

    schema = OUTPUT_SCHEMAS["execute_query"]
    validate({"status": "success", "results": [46219]}, schema)
    validate({"status": "success", "results": [{"type": "1", "cnt": 27777}]}, schema)
    validate({"status": "success", "results": [["a", "b"]]}, schema)
