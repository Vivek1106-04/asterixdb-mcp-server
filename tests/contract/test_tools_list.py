"""Contract tests: the advertised MCP surface matches its specification.

Verifies tools/list, resources/list, and prompts/list shapes against the
functional design so accidental drift in names or schemas is caught in CI.
"""

from __future__ import annotations

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def server() -> object:
    return build_server(Settings(cc_base_url="http://test-cc:19002"))


async def test_advertises_exactly_the_expected_tools(server) -> None:
    tools = await server.list_tools()
    assert {t.name for t in tools} == {
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


async def test_execute_query_schema_requires_statement_and_hides_readonly(server) -> None:
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["execute_query"].inputSchema
    props = schema["properties"]
    # statement is required; the egress-controlled params are NOT client-settable.
    assert "statement" in schema["required"]
    assert "readonly" not in props
    assert "timeout" not in props
    # Pagination + tuning knobs are exposed with the camelCase contract names.
    assert {"offset", "limit", "compilerParameters", "maxWarnings"} <= set(props)


async def test_advertises_expected_resources(server) -> None:
    uris = {str(r.uri) for r in await server.list_resources()}
    assert "asterixdb://version" in uris
    assert "asterixdb://cluster/status" in uris
    assert "asterixdb://config-parameters" in uris
    assert "asterixdb://dataverses" in uris
    assert "asterixdb://cluster/diagnostics" in uris
    for ref in (
        "sqlpp-syntax",
        "builtin-functions",
        "index-types",
        "type-system",
        "error-codes",
        "query-examples",
    ):
        assert f"asterixdb://reference/{ref}" in uris


async def test_advertises_power_prompts(server) -> None:
    names = {p.name for p in await server.list_prompts()}
    assert {
        "build_aggregation_query",
        "analyze_query_performance",
        "recommend_indexes",
        "explore_nested_data",
        "explain_error",
    } <= names


async def test_advertises_analyze_dataverse_prompt(server) -> None:
    prompts = {p.name for p in await server.list_prompts()}
    assert "analyze_dataverse" in prompts
