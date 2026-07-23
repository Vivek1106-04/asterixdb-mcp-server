"""ASTERIXDB_MCP_MEMORY_ENABLED=false: the memory surface disappears entirely.

The flag exists so operators (and benchmarks) can run the plain catalog/query
gateway with zero memory behavior: no memory tools, no session briefing, no
ambient learned-note recall, no episodic capture, no startup maintenance.
"""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.maintenance import run_startup_maintenance
from asterixdb_mcp.server import MEMORY_TOOL_NAMES, SERVER_INSTRUCTIONS_NO_MEMORY, build_server
from asterixdb_mcp.tools import ToolResult
from asterixdb_mcp.tools.briefing import BriefingState, maybe_attach_briefing
from asterixdb_mcp.tools.get_schema import run_get_schema
from asterixdb_mcp.tools.memory_capture import CaptureState, capture_query_outcome
from asterixdb_mcp.tools.memory_notes import attach_statement_notes
from asterixdb_mcp.tools.sample_dataset import run_sample_dataset
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


@pytest.fixture
def disabled_settings(settings: Settings) -> Settings:
    # memory_write_enabled=True on purpose: the master switch must override it.
    return settings.model_copy(update={"memory_enabled": False, "memory_write_enabled": True})


def test_memory_enabled_defaults_true() -> None:
    assert Settings(cc_base_url="http://test-cc:19002").memory_enabled is True


def test_memory_enabled_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTERIXDB_MCP_MEMORY_ENABLED", "false")
    assert Settings(cc_base_url="http://test-cc:19002").memory_enabled is False


async def test_memory_tools_absent_and_instructions_swapped(
    disabled_settings: Settings,
) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"status": "success", "results": []})
        ),
        base_url=disabled_settings.cc_base_url,
    )
    server = build_server(disabled_settings, http=http)
    names = {t.name for t in await server.list_tools()}
    assert names.isdisjoint(MEMORY_TOOL_NAMES)
    assert "execute_query" in names  # the rest of the surface is intact
    assert server.instructions == SERVER_INSTRUCTIONS_NO_MEMORY
    assert "memory" not in server.instructions.lower()


async def test_briefing_skipped(disabled_settings: Settings) -> None:
    cap = make_capturing_cc(disabled_settings)
    result = ToolResult(text="tool output", structured={"status": "success"})
    attached = await maybe_attach_briefing(
        cap.client, disabled_settings, BriefingState(), result
    )
    assert attached is result
    assert cap.requests == []


async def test_ambient_statement_notes_skipped(disabled_settings: Settings) -> None:
    cap = make_capturing_cc(disabled_settings)
    result = ToolResult(text="rows", structured={"status": "success"})
    attached = await attach_statement_notes(
        cap.client,
        "ccid",
        "SELECT * FROM dv.ds;",
        result,
        settings=disabled_settings,
    )
    assert attached is result
    assert cap.requests == []


def _schema_handler(req: httpx.Request) -> httpx.Response:
    form = req.content.decode()
    if "Metadata.`Dataset`" in form:
        rows = [
            {
                "DataverseName": "dv",
                "DatasetName": "ds",
                "DatatypeDataverseName": "dv",
                "DatatypeName": "dsType",
                "InternalDetails": {"PrimaryKey": [["id"]]},
            }
        ]
    elif "Metadata.`Datatype`" in form:
        rows = [
            {
                "DatatypeName": "dsType",
                "Derived": {"Record": {"Fields": [{"FieldName": "id", "FieldType": "string"}]}},
            }
        ]
    elif "Metadata.`Index`" in form:
        rows = []
    else:
        rows = [{"id": "r1"}]
    return httpx.Response(200, json={"status": "success", "results": rows})


async def test_get_schema_carries_no_memory_notes_or_hints(
    disabled_settings: Settings,
) -> None:
    cap = make_capturing_cc(disabled_settings, handler=_schema_handler)
    result = await run_get_schema(cap.client, disabled_settings, dataverse="dv", dataset="ds")
    assert result.structured["status"] == "success"
    assert "learnedNotes" not in result.structured
    assert "memory" not in result.text.lower()
    for req in cap.requests:  # no query ever touches the memory store
        assert "AgentMemory" not in req.content.decode()


def _sample_handler(req: httpx.Request) -> httpx.Response:
    stmt = req.content.decode()
    rows = [{"DataverseName": "dv", "DatasetName": "ds"}] if "Metadata" in stmt else [{"id": "r1"}]
    return httpx.Response(200, json={"status": "success", "results": rows})


async def test_sample_dataset_carries_no_memory_notes_or_hints(
    disabled_settings: Settings,
) -> None:
    cap = make_capturing_cc(disabled_settings, handler=_sample_handler)
    result = await run_sample_dataset(cap.client, disabled_settings, dataverse="dv", dataset="ds")
    assert result.structured["status"] == "success"
    assert "learnedNotes" not in result.structured
    assert "memory" not in result.text.lower()
    for req in cap.requests:
        assert "AgentMemory" not in req.content.decode()


async def test_capture_is_a_no_op(disabled_settings: Settings, tmp_path) -> None:
    logged = disabled_settings.model_copy(update={"session_log_dir": str(tmp_path)})
    cap = make_capturing_cc(logged)
    await capture_query_outcome(
        cap.client,
        logged,
        CaptureState(),
        statement="SELECT * FROM dv.ds;",
        result_error=None,
    )
    assert cap.requests == []  # no episodic insert
    assert list(tmp_path.iterdir()) == []  # no JSONL buffer either


async def test_startup_maintenance_is_a_no_op(disabled_settings: Settings) -> None:
    # Would need a reachable CC otherwise; returning without touching the
    # network IS the assertion.
    await run_startup_maintenance(disabled_settings)
