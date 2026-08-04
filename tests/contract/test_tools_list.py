"""Contract tests: the advertised MCP surface matches its specification.

Verifies tools/list, resources/list, and prompts/list shapes against the
functional design so accidental drift in names or schemas is caught in CI.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from mcp import types

from asterixdb_mcp.config import Settings
from asterixdb_mcp.server import build_server

pytestmark = pytest.mark.anyio

# Completion handlers register against the MCP method name rather than a request
# type, so this is the key the low-level server files them under.
COMPLETION_METHOD = "completion/complete"


@pytest.fixture
def server() -> object:
    return build_server(Settings(cc_base_url="http://test-cc:19002"))


async def test_completion_handler_completes_dataverse_argument() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{"DataverseName": "Sales"}, {"DataverseName": "Shop"}]}
        )

    settings = Settings(cc_base_url="http://test-cc:19002")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.cc_base_url)
    server = build_server(settings, http=http)
    low = server._lowlevel_server
    complete = low.get_request_handler(COMPLETION_METHOD)
    assert complete is not None, "no completion/complete handler registered"

    params = types.CompleteRequestParams(
        ref=types.PromptReference(type="ref/prompt", name="analyze_dataverse"),
        argument=types.CompletionArgument(name="dataverse", value="s"),
    )
    result = await complete.handler(None, params)
    assert set(result.completion.values) == {"Sales", "Shop"}


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


async def test_every_tool_advertises_behavioral_annotations(server) -> None:
    # High-end clients read annotations to decide auto-invocation. Every tool must
    # carry hints; the gateway never destroys data, so destructiveHint is False
    # across the whole surface.
    tools = await server.list_tools()
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.title, tool.name
        assert tool.annotations.destructive_hint is False, tool.name


async def test_read_only_tools_are_marked_read_only(server) -> None:
    tools = {t.name: t for t in await server.list_tools()}
    # cancel_query mutates execution state; memory_write and remember_preference
    # persist into the settings-gated AgentMemory.Memory store. Everything else is
    # read-only.
    for name, tool in tools.items():
        expected = name not in ("cancel_query", "memory_write", "remember_preference")
        assert tool.annotations.read_only_hint is expected, name


async def test_open_world_and_idempotency_hints(server) -> None:
    tools = {t.name: t for t in await server.list_tools()}
    # get_reference reads in-gateway static docs; it is the only closed-world tool.
    assert tools["get_reference"].annotations.open_world_hint is False
    assert tools["execute_query"].annotations.open_world_hint is True
    # Each submit allocates a fresh async handle, so it is not idempotent.
    assert tools["submit_async_query"].annotations.idempotent_hint is False
    assert tools["execute_query"].annotations.idempotent_hint is True


async def test_every_tool_advertises_an_output_schema(server) -> None:
    # High-end clients read outputSchema to anticipate result shape and chain calls.
    tools = await server.list_tools()
    for tool in tools:
        assert tool.output_schema is not None, tool.name
        assert tool.output_schema["type"] == "object", tool.name


async def test_output_schema_is_advertised_not_enforced_on_errors() -> None:
    # A failing call must surface its error, never be rejected for not matching
    # the advertised success schema. Error results carry no structured content
    # (a client validates structuredContent against the success outputSchema, so
    # an error envelope there would be rejected and mask the real error); the
    # classified errorType lives in the text content instead.
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": [{"msg": "boom"}]})

    settings = Settings(cc_base_url="http://test-cc:19002")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.cc_base_url)
    server = build_server(settings, http=http)
    result = await server.call_tool("get_schema", {"dataverse": "D", "dataset": "X"})
    assert result.is_error is True
    assert result.structured_content is None
    assert result.content[0].text.split(":")[0].isupper()


async def test_execute_query_schema_requires_statement_and_hides_readonly(server) -> None:
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["execute_query"].input_schema
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
        "query-hints",
    ):
        assert f"asterixdb://reference/{ref}" in uris


async def test_advertises_resource_templates(server) -> None:
    templates = {t.uri_template for t in await server.list_resource_templates()}
    assert templates == {
        "asterixdb://schema/{dataverse}/{dataset}",
        "asterixdb://dataverse/{dataverse}",
        "asterixdb://sample/{dataverse}/{dataset}",
        "asterixdb://datasets/{dataverse}",
        "asterixdb://indexes/{dataverse}/{dataset}",
        "asterixdb://indexes/{dataverse}",
        # Not catalog context like the others: this is how an overflow artifact
        # is retrievable on a transport with no HTTP download route.
        "asterixdb://artifacts/{artifact_id}",
    }


async def test_resource_template_completion_resolves_dataset_argument() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"DatasetName": "Orders", "DataverseName": "Sales"}]},
        )

    settings = Settings(cc_base_url="http://test-cc:19002")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.cc_base_url)
    server = build_server(settings, http=http)
    low = server._lowlevel_server

    params = types.CompleteRequestParams(
        ref=types.ResourceTemplateReference(
            type="ref/resource", uri="asterixdb://schema/{dataverse}/{dataset}"
        ),
        argument=types.CompletionArgument(name="dataset", value="ord"),
        context=types.CompletionContext(arguments={"dataverse": "Sales"}),
    )
    complete = low.get_request_handler(COMPLETION_METHOD)
    assert complete is not None
    result = await complete.handler(None, params)
    assert result.completion.values == ["Orders"]


async def test_each_resource_template_reads_through_the_server() -> None:
    # Drives every template closure in server.py end to end.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "results": [{"DatasetName": "Orders", "DataverseName": "Sales"}],
            },
        )

    settings = Settings(cc_base_url="http://test-cc:19002")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.cc_base_url)
    server = build_server(settings, http=http)

    for uri in (
        "asterixdb://schema/Sales/Orders",
        "asterixdb://dataverse/Sales",
        "asterixdb://sample/Sales/Orders",
        "asterixdb://datasets/Sales",
        "asterixdb://indexes/Sales/Orders",
        "asterixdb://indexes/Sales",
    ):
        contents = await server.read_resource(uri)
        body = next(iter(contents)).content
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


# --- prompt-cache stability -------------------------------------------------
#
# The tools block is the first thing in every request and is re-sent on every
# turn - measured at ~12.9k tokens across 29 tools. Clients that cache
# explicitly (Anthropic's cache_control) only get that discount while the block
# is byte-identical between requests. A timestamp, uuid, or dict-ordered field
# slipped into any description silently costs every such client ~10x on input,
# and nothing errors. These tests are the only thing that would notice.


def _serialize(tools: list[types.Tool]) -> str:
    """Serialize exactly as the wire does: declared order, model field order.

    ``by_alias`` is the load-bearing argument. The model fields are snake_case in
    Python but serialize to camelCase (``inputSchema``, ``readOnlyHint``) on the
    wire; dumping without it would silently measure a byte string no client ever
    receives, and the cache-stability guarantee below would be about nothing.
    """
    return json.dumps([t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools])


async def test_tools_block_is_byte_identical_across_builds() -> None:
    # Arrange: two servers built independently, same settings
    settings = Settings(cc_base_url="http://test-cc:19002")

    # Act
    first = _serialize(await build_server(settings).list_tools())
    second = _serialize(await build_server(settings).list_tools())

    # Assert
    assert first == second, (
        "tools/list is not deterministic between builds - prompt caching is "
        "defeated for every client that caches explicitly."
    )


@pytest.mark.parametrize(
    ("label", "pattern"),
    [
        ("ISO timestamp", r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"),
        ("uuid", r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
        ("object address", r"0x[0-9a-f]{8,}"),
    ],
)
async def test_tools_block_carries_no_volatile_content(server, label, pattern) -> None:
    """Determinism within one process can still hide content that varies per run."""
    blob = _serialize(await server.list_tools())
    assert not re.search(pattern, blob), f"tool schemas contain a per-run {label}"
