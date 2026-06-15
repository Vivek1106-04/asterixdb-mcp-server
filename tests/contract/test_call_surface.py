"""End-to-end binding tests: call the surface through FastMCP with a mock CC.

These drive the actual ``server.py`` closures (tools, resources, prompt) and the
ToolResult -> CallToolResult conversion, so the FastMCP wiring is covered, not
just the SDK-agnostic cores.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest
from mcp import types

from asterixdb_mcp.config import Settings
from asterixdb_mcp.server import build_server

pytestmark = pytest.mark.anyio


def _server_with_mock(handler: object, **settings_overrides: object) -> object:
    settings = Settings(
        cc_base_url="http://test-cc:19002",
        agent_session_id="sess-test",
        **settings_overrides,
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=settings.cc_base_url,
    )
    return build_server(settings, http=http)


async def test_execute_query_call_returns_structured_result() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "results": [{"n": 1}]})

    server = _server_with_mock(handler)
    result = await server.call_tool("execute_query", {"statement": "SELECT 1;"})

    assert isinstance(result, types.CallToolResult)
    assert result.isError is False
    assert result.structuredContent["status"] == "success"
    assert result.structuredContent["rowsReturned"] == 1


async def test_success_text_mirrors_structured_payload() -> None:
    # Text-first clients (e.g. Antigravity) render only content[].text. The
    # structured payload must therefore also appear in the text block so the
    # actual rows are visible, not just a "Returned N row(s)." summary.
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "results": [{"n": 42}]})

    server = _server_with_mock(handler)
    result = await server.call_tool("execute_query", {"statement": "SELECT 42;"})
    text = result.content[0].text
    assert "```json" in text
    assert '"n":42' in text  # compact separators, no spaces
    # The human summary is still the first line.
    assert text.splitlines()[0].startswith("Returned 1 row(s)")


async def test_error_text_has_no_json_mirror() -> None:
    server = _server_with_mock(lambda r: httpx.Response(500, text="boom"))
    result = await server.call_tool("execute_query", {"statement": "SELECT 1;"})
    assert result.isError is True
    assert result.structuredContent is None
    assert "```json" not in result.content[0].text


def test_text_with_payload_passes_through_empty_structured() -> None:
    from asterixdb_mcp.server import _text_with_payload
    from asterixdb_mcp.tools import ToolResult

    # A success result with no structured payload yields the bare summary text.
    assert _text_with_payload(ToolResult(text="done", structured={})) == "done"


async def test_readonly_true_reaches_cc_through_the_tool() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(parse_qs(req.content.decode()).get("readonly", [""])[0])
        return httpx.Response(200, json={"status": "success", "results": []})

    server = _server_with_mock(handler)
    await server.call_tool("execute_query", {"statement": "SELECT 1;"})
    # execute_query now does a compile-only columnar preflight then the real run;
    # both hops must carry readonly=true.
    assert seen and all(value == "true" for value in seen)


async def test_version_resource_read() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Git revision": "deadbeef"})

    server = _server_with_mock(handler)
    contents = list(await server.read_resource("asterixdb://version"))
    payload = json.loads(contents[0].content)
    assert payload["asterixdb"]["version"] == "deadbeef"


async def test_analyze_dataverse_prompt_get() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "success", "results": [{"DataverseName": "DV", "DatasetName": "a"}]},
        )

    server = _server_with_mock(handler)
    result = await server.get_prompt("analyze_dataverse", {"dataverse": "DV"})
    rendered = result.messages[0].content.text
    assert "Exploring Dataverse `DV`" in rendered


async def test_get_schema_and_list_datasets_calls() -> None:
    dataset = {
        "DataverseName": "DV",
        "DatasetName": "Events",
        "DatatypeName": "T",
        "DatasetFormat": {"Format": "row"},
        "InternalDetails": {"PrimaryKey": [["id"]]},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        stmt = parse_qs(req.content.decode())["statement"][0]
        rows = [dataset] if "`Dataset`" in stmt else []
        return httpx.Response(200, json={"status": "success", "results": rows})

    server = _server_with_mock(handler)

    schema = await server.call_tool("get_schema", {"dataverse": "DV", "dataset": "Events"})
    assert schema.structuredContent["primaryKey"] == ["id"]

    listing = await server.call_tool("list_datasets", {})
    assert listing.structuredContent["totalDatasets"] == 1


async def test_list_dataverses_call() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "success", "results": [{"DataverseName": "Yelp"}]}
        )

    server = _server_with_mock(handler)
    result = await server.call_tool("list_dataverses", {})
    assert result.structuredContent["count"] == 1
    assert result.structuredContent["dataverses"][0]["dataverse"] == "Yelp"


async def test_describe_dataverse_call() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        stmt = parse_qs(req.content.decode())["statement"][0]
        if "ORDER BY" in stmt:
            rows: list = [{"DataverseName": "Yelp", "DatasetName": "Review"}]
        elif "`Datatype`" in stmt:
            rows = [{"Derived": {"Record": {"Fields": []}}}]
        elif "`Index`" in stmt:
            rows = []
        else:
            rows = [
                {
                    "DataverseName": "Yelp",
                    "DatasetName": "Review",
                    "DatatypeName": "T",
                    "DatasetFormat": {"Format": "row"},
                    "InternalDetails": {"PrimaryKey": [["id"]]},
                }
            ]
        return httpx.Response(200, json={"status": "success", "results": rows})

    server = _server_with_mock(handler)
    result = await server.call_tool("describe_dataverse", {"dataverse": "Yelp"})
    assert result.structuredContent["describedCount"] == 1


async def test_sample_dataset_call() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        stmt = parse_qs(req.content.decode())["statement"][0]
        if "Metadata" in stmt:
            rows: list = [{"DataverseName": "Yelp", "DatasetName": "Business"}]
        else:
            rows = [{"business_id": "x", "state": "NV"}]
        return httpx.Response(200, json={"status": "success", "results": rows})

    server = _server_with_mock(handler)
    result = await server.call_tool("sample_dataset", {"dataverse": "Yelp", "dataset": "Business"})
    assert result.structuredContent["rowsReturned"] == 1
    assert result.structuredContent["dataset"] == "Business"


async def test_cluster_status_resource_read() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": "ACTIVE", "ncs": [], "cc": {}})

    server = _server_with_mock(handler)
    contents = list(await server.read_resource("asterixdb://cluster/status"))
    payload = json.loads(contents[0].content)
    assert payload["reachable"] is True


async def test_lazy_client_creation_without_injected_http() -> None:
    # No injected client -> _client() lazily builds one against an unreachable URL.
    server = build_server(Settings(cc_base_url="http://127.0.0.1:1", request_timeout_s=1.0))
    result = await server.call_tool("execute_query", {"statement": "SELECT 1;"})
    assert result.isError is True
    # Error envelopes carry no structured content (it would fail outputSchema
    # validation); the classified errorType is in the text content.
    assert result.structuredContent is None
    assert result.content[0].text.split(":")[0] in {"INTERNAL", "TIMEOUT"}


async def test_submit_async_query_call() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "running", "handle": "/query/service/status/0-1"}
        )

    server = _server_with_mock(handler)
    result = await server.call_tool(
        "submit_async_query", {"statement": "SELECT * FROM Big LIMIT 5;"}
    )
    assert result.isError is False
    # The one lifecycle id is the clientContextID; no raw handle is surfaced.
    assert result.structuredContent["clientContextID"].startswith("sess-test::")


async def test_async_lifecycle_submit_wait_fetch_by_client_context_id() -> None:
    # One server instance shares its audit log across calls, so the same
    # clientContextID flows submit -> wait -> fetch with no handle juggling.
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST":
            return httpx.Response(
                200, json={"status": "running", "handle": "/query/service/status/0-1"}
            )
        if path == "/query/service/status/0-1":
            return httpx.Response(
                200, json={"status": "success", "handle": "/query/service/result/0-1"}
            )
        return httpx.Response(200, json={"status": "success", "results": [{"n": 1}]})

    server = _server_with_mock(handler)

    submitted = await server.call_tool(
        "submit_async_query", {"statement": "SELECT * FROM Big LIMIT 5;"}
    )
    ccid = submitted.structuredContent["clientContextID"]

    waited = await server.call_tool(
        "wait_on_async_query", {"clientContextID": ccid, "timeoutMs": 0}
    )
    assert waited.structuredContent["done"] is True

    fetched = await server.call_tool("fetch_query_result", {"clientContextID": ccid})
    assert fetched.structuredContent["rowsReturned"] == 1


async def test_wait_unknown_client_context_id_call() -> None:
    server = _server_with_mock(lambda r: httpx.Response(200, json={}))
    result = await server.call_tool(
        "wait_on_async_query", {"clientContextID": "sess-test::_::missing"}
    )
    assert result.isError is True
    assert result.structuredContent is None
    assert result.content[0].text.split(":")[0] == "NOT_FOUND"


async def test_cancel_query_call() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "DELETE"
        return httpx.Response(200)

    server = _server_with_mock(handler)
    result = await server.call_tool("cancel_query", {"clientContextID": "sess-test::_::u"})
    assert result.structuredContent["cancelled"] is True


async def test_validate_syntax_call() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success"})

    server = _server_with_mock(handler)
    result = await server.call_tool("validate_syntax", {"statement": "SELECT 1;"})
    assert result.structuredContent["valid"] is True


async def test_explain_query_call() -> None:
    plan = {
        "status": "success",
        "plans": {
            "optimizedLogicalPlan": {
                "operator": "data-scan",
                "data-source": "DV.Events",
                "inputs": [],
            }
        },
    }

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=plan)

    server = _server_with_mock(handler)
    result = await server.call_tool("explain_query", {"statement": "SELECT 1;"})
    assert result.structuredContent["plan"]["dataSources"] == ["DV.Events"]


async def test_config_parameters_resource_read() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    server = _server_with_mock(handler)
    contents = list(await server.read_resource("asterixdb://config-parameters"))
    payload = json.loads(contents[0].content)
    assert any(p["name"] == "compiler.parallelism" for p in payload["compilerParameters"])
    assert payload["concurrency"]["syncPermits"] == 3


async def test_execute_query_sheds_load_when_sync_pool_full() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_req: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, json={"status": "success", "results": []})

    server = _server_with_mock(handler, sync_permits=1)
    first = asyncio.create_task(server.call_tool("execute_query", {"statement": "SELECT 1;"}))
    await started.wait()  # first call holds the only sync permit

    # The second call finds the pool full and is shed with NOT_READY.
    second = await server.call_tool("execute_query", {"statement": "SELECT 1;"})
    assert second.isError is True
    assert second.structuredContent is None
    assert second.content[0].text.split(":")[0] == "NOT_READY"

    release.set()
    await first


async def test_check_index_usage_call() -> None:
    plan = {
        "status": "success",
        "plans": {
            "optimizedLogicalPlan": {
                "operator": "data-scan",
                "data-source": "Sales.Orders",
                "inputs": [],
            }
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if form.get("compile-only") == "true":
            return httpx.Response(200, json=plan)
        return httpx.Response(200, json={"status": "success", "results": []})

    server = _server_with_mock(handler)
    result = await server.call_tool(
        "check_index_usage", {"statement": "SELECT * FROM Sales.Orders LIMIT 5;"}
    )
    assert result.isError is False
    assert result.structuredContent["usesFullScan"] is True


async def test_list_functions_call() -> None:
    empty = {"status": "success", "results": []}
    server = _server_with_mock(lambda r: httpx.Response(200, json=empty))
    result = await server.call_tool("list_functions", {"language": "INTERNAL", "limit": 5})
    assert result.structuredContent["total"] > 0


async def test_get_function_call_builtin() -> None:
    server = _server_with_mock(lambda r: httpx.Response(200, json={"status": "success"}))
    result = await server.call_tool("get_function", {"name": "count"})
    assert result.structuredContent["scope"] == "builtin"


async def test_search_metadata_call() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        stmt = parse_qs(req.content.decode()).get("statement", [""])[0]
        if "Metadata.`Dataset`" in stmt:
            row = {"DatasetName": "orders", "DataverseName": "S"}
            return httpx.Response(200, json={"status": "success", "results": [row]})
        return httpx.Response(200, json={"status": "success", "results": []})

    server = _server_with_mock(handler)
    result = await server.call_tool("search_metadata", {"query": "orders"})
    assert result.structuredContent["totalMatches"] == 1


async def test_get_cluster_status_call() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/admin/version":
            return httpx.Response(200, json={"Git revision": "abc123"})
        return httpx.Response(200, json={"state": "ACTIVE", "ncs": [{"node_id": "nc1"}]})

    server = _server_with_mock(handler)
    result = await server.call_tool("get_cluster_status", {})
    assert result.structuredContent["state"] == "ACTIVE"
    assert result.structuredContent["nodes"][0]["nodeId"] == "nc1"


async def test_get_node_details_call() -> None:
    server = _server_with_mock(lambda r: httpx.Response(200, json={"nodeId": "nc1"}))
    result = await server.call_tool("get_node_details", {"node": "nc1"})
    assert result.structuredContent["node"] == "nc1"


async def test_dataverses_resource_read() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "success", "results": [{"DataverseName": "Sales"}]}
        )

    server = _server_with_mock(handler)
    contents = list(await server.read_resource("asterixdb://dataverses"))
    payload = json.loads(contents[0].content)
    assert payload["dataverses"][0]["dataverse"] == "Sales"


async def test_cluster_diagnostics_resource_read() -> None:
    server = _server_with_mock(lambda r: httpx.Response(200, json={"nodes": [{"id": "nc1"}]}))
    contents = list(await server.read_resource("asterixdb://cluster/diagnostics"))
    payload = json.loads(contents[0].content)
    assert payload["nodes"] == [{"id": "nc1"}]


@pytest.mark.parametrize(
    "uri",
    [
        "asterixdb://reference/sqlpp-syntax",
        "asterixdb://reference/builtin-functions",
        "asterixdb://reference/index-types",
        "asterixdb://reference/type-system",
        "asterixdb://reference/error-codes",
        "asterixdb://reference/query-examples",
    ],
)
async def test_reference_resources_read(uri: str) -> None:
    server = _server_with_mock(lambda r: httpx.Response(200, json={}))
    contents = list(await server.read_resource(uri))
    payload = json.loads(contents[0].content)
    assert payload["version"]


async def test_power_prompts_get() -> None:
    server = _server_with_mock(lambda r: httpx.Response(200, json={}))
    agg = await server.get_prompt("build_aggregation_query", {"dataverse": "D", "dataset": "S"})
    assert "GROUP BY" in agg.messages[0].content.text

    perf = await server.get_prompt("analyze_query_performance", {})
    assert "metrics" in perf.messages[0].content.text

    rec = await server.get_prompt("recommend_indexes", {"dataverse": "D", "dataset": "S"})
    assert "check_index_usage" in rec.messages[0].content.text

    nested = await server.get_prompt("explore_nested_data", {"dataverse": "D", "dataset": "S"})
    assert "UNNEST" in nested.messages[0].content.text

    err = await server.get_prompt("explain_error", {"error": "ASX1077"})
    assert "ASX1077" in err.messages[0].content.text


async def test_get_reference_call_returns_static_topic() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)  # static tool must not touch the CC

    server = _server_with_mock(handler)
    result = await server.call_tool("get_reference", {"topic": "index-types"})
    assert result.structuredContent["topic"] == "index-types"
    assert result.isError is False


async def test_select_value_scalar_result_survives_and_validates() -> None:
    # SELECT VALUE COUNT(*) yields a scalar row [46219]. The result must flow
    # through unchanged AND pass the advertised outputSchema the client checks.
    from jsonschema import validate

    from asterixdb_mcp.output_schemas import OUTPUT_SCHEMAS

    def handler(req: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if form.get("compile-only") == "true":
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(200, json={"status": "success", "results": [46219]})

    server = _server_with_mock(handler)
    result = await server.call_tool(
        "execute_query", {"statement": "SELECT VALUE COUNT(*) FROM DV.Big;"}
    )
    assert result.isError is False
    assert result.structuredContent["results"] == [46219]
    validate(result.structuredContent, OUTPUT_SCHEMAS["execute_query"])


async def test_error_result_ships_no_structured_content() -> None:
    # A gateway error must not carry structured content: a client validates it
    # against the success outputSchema and would mask the real error. The
    # classified errorType stays in the text content.
    server = _server_with_mock(lambda r: httpx.Response(500, text="boom"))
    result = await server.call_tool("execute_query", {"statement": "SELECT VALUE 1;"})
    assert result.isError is True
    assert result.structuredContent is None
    assert result.content[0].text.split(":")[0].isupper()


async def test_database_health_check_call_returns_findings() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        statement = parse_qs(req.content.decode()).get("statement", [""])[0]
        if "Metadata.`Index`" in statement:
            return httpx.Response(200, json={"status": "success", "results": [
                {"IndexName": "a", "DatasetName": "Orders", "DataverseName": "S",
                 "IndexStructure": "BTREE", "SearchKey": [["city"]], "IsPrimary": False},
                {"IndexName": "b", "DatasetName": "Orders", "DataverseName": "S",
                 "IndexStructure": "BTREE", "SearchKey": [["city"]], "IsPrimary": False},
            ]})
        return httpx.Response(200, json={"status": "success", "results": []})

    server = _server_with_mock(handler)
    result = await server.call_tool("database_health_check", {})
    assert result.isError is False
    assert result.structuredContent["findingsCount"] == 1


async def test_query_history_records_and_returns_execute_query() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "results": [{"n": 1}]})

    server = _server_with_mock(handler)
    await server.call_tool("execute_query", {"statement": "SELECT 1;"})
    history = await server.call_tool("get_query_history", {})
    assert history.isError is False
    assert history.structuredContent["count"] == 1
    entry = history.structuredContent["queries"][0]
    assert entry["tool"] == "execute_query"
    assert entry["outcome"] == "SUCCESS"
    assert entry["statement"] == "SELECT 1;"


def test_main_builds_and_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.server.fastmcp import FastMCP

    from asterixdb_mcp import server as server_module

    ran: dict[str, bool] = {}
    monkeypatch.setattr(FastMCP, "run", lambda self, *a, **k: ran.setdefault("ran", True))
    server_module.main()
    assert ran["ran"] is True
