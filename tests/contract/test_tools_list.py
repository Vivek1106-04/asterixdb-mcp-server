"""Contract tests: the advertised MCP surface matches its specification.

Verifies tools/list, resources/list, and prompts/list shapes against the
functional design so accidental drift in names or schemas is caught in CI.
"""

from __future__ import annotations

import httpx
import pytest
from mcp import types

from asterixdb_mcp.config import Settings
from asterixdb_mcp.server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def server() -> object:
    return build_server(Settings(cc_base_url="http://test-cc:19002"))


async def test_completion_handler_completes_dataverse_argument() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{"DataverseName": "Sales"}, {"DataverseName": "Shop"}]}
        )

    settings = Settings(cc_base_url="http://test-cc:19002")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.cc_base_url
    )
    server = build_server(settings, http=http)
    low = server._mcp_server
    assert types.CompleteRequest in low.request_handlers

    request = types.CompleteRequest(
        method="completion/complete",
        params=types.CompleteRequestParams(
            ref=types.PromptReference(type="ref/prompt", name="analyze_dataverse"),
            argument=types.CompletionArgument(name="dataverse", value="s"),
        ),
    )
    result = await low.request_handlers[types.CompleteRequest](request)
    assert set(result.root.completion.values) == {"Sales", "Shop"}


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


async def test_every_tool_advertises_behavioral_annotations(server) -> None:
    # High-end clients read annotations to decide auto-invocation. Every tool must
    # carry hints; the gateway never destroys data, so destructiveHint is False
    # across the whole surface.
    tools = await server.list_tools()
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.title, tool.name
        assert tool.annotations.destructiveHint is False, tool.name


async def test_read_only_tools_are_marked_read_only(server) -> None:
    tools = {t.name: t for t in await server.list_tools()}
    # cancel_query mutates server-side execution state; everything else is read-only.
    for name, tool in tools.items():
        expected = name != "cancel_query"
        assert tool.annotations.readOnlyHint is expected, name


async def test_open_world_and_idempotency_hints(server) -> None:
    tools = {t.name: t for t in await server.list_tools()}
    # get_reference reads in-gateway static docs; it is the only closed-world tool.
    assert tools["get_reference"].annotations.openWorldHint is False
    assert tools["execute_query"].annotations.openWorldHint is True
    # Each submit allocates a fresh async handle, so it is not idempotent.
    assert tools["submit_async_query"].annotations.idempotentHint is False
    assert tools["execute_query"].annotations.idempotentHint is True


async def test_every_tool_advertises_an_output_schema(server) -> None:
    # High-end clients read outputSchema to anticipate result shape and chain calls.
    tools = await server.list_tools()
    for tool in tools:
        assert tool.outputSchema is not None, tool.name
        assert tool.outputSchema["type"] == "object", tool.name


async def test_output_schema_is_advertised_not_enforced_on_errors() -> None:
    # A failing call must still return its error envelope, never be rejected for
    # not matching the advertised success schema.
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": [{"msg": "boom"}]})

    settings = Settings(cc_base_url="http://test-cc:19002")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.cc_base_url
    )
    server = build_server(settings, http=http)
    result = await server.call_tool("get_schema", {"dataverse": "D", "dataset": "X"})
    assert result.isError is True
    assert "errorType" in result.structuredContent


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


async def test_advertises_resource_templates(server) -> None:
    templates = {t.uriTemplate for t in await server.list_resource_templates()}
    assert templates == {
        "asterixdb://schema/{dataverse}/{dataset}",
        "asterixdb://dataverse/{dataverse}",
        "asterixdb://sample/{dataverse}/{dataset}",
        "asterixdb://datasets/{dataverse}",
    }


async def test_resource_template_completion_resolves_dataset_argument() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [{"DatasetName": "Orders", "DataverseName": "Sales"}]
            },
        )

    settings = Settings(cc_base_url="http://test-cc:19002")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.cc_base_url
    )
    server = build_server(settings, http=http)
    low = server._mcp_server

    request = types.CompleteRequest(
        method="completion/complete",
        params=types.CompleteRequestParams(
            ref=types.ResourceTemplateReference(
                type="ref/resource", uri="asterixdb://schema/{dataverse}/{dataset}"
            ),
            argument=types.CompletionArgument(name="dataset", value="ord"),
            context=types.CompletionContext(arguments={"dataverse": "Sales"}),
        ),
    )
    result = await low.request_handlers[types.CompleteRequest](request)
    assert result.root.completion.values == ["Orders"]


async def test_each_resource_template_reads_through_the_server() -> None:
    # Drives the four template closures in server.py end to end.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "results": [{"DatasetName": "Orders", "DataverseName": "Sales"}],
            },
        )

    settings = Settings(cc_base_url="http://test-cc:19002")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.cc_base_url
    )
    server = build_server(settings, http=http)

    for uri in (
        "asterixdb://schema/Sales/Orders",
        "asterixdb://dataverse/Sales",
        "asterixdb://sample/Sales/Orders",
        "asterixdb://datasets/Sales",
    ):
        contents = await server.read_resource(uri)
        body = list(contents)[0].content
        assert "status" in body, uri


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
