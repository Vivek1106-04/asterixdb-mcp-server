"""Unit tests for get_query_history and the record_query recorder."""

from __future__ import annotations

import itertools

import pytest

from asterixdb_mcp.audit_log import AuditLog
from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType, GatewayError
from asterixdb_mcp.tools import ToolResult
from asterixdb_mcp.tools.query_history import (
    MAX_LIMIT,
    record_query,
    run_get_query_history,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def settings() -> Settings:
    return Settings(cc_base_url="http://test-cc:19002", agent_session_id="sess-test")


def _stepping_clock():
    """A monotonic clock that advances one tick per call (deterministic ordering)."""
    counter = itertools.count(1)
    return lambda: float(next(counter))


# record_query


def test_record_success_captures_rows_and_ccid(settings: Settings) -> None:
    audit = AuditLog(ttl_s=900)
    result = ToolResult(
        text="ok",
        structured={"status": "success", "clientContextID": "sess-test::_::u1", "rowsReturned": 5},
    )
    record_query(audit, settings, tool="execute_query", statement="SELECT 1;",
                 dataverse="S", result=result)
    entry = audit.get("sess-test::_::u1")
    assert entry is not None
    assert entry.outcome == "SUCCESS"
    assert entry.rows_returned == 5
    assert entry.tool == "execute_query"
    assert entry.dataverse == "S"


def test_record_error_mints_ccid_and_keeps_error_fields(settings: Settings) -> None:
    audit = AuditLog(ttl_s=900)
    err = GatewayError(ErrorType.SEMANTIC_ERROR, "cannot find dataset")
    record_query(audit, settings, tool="execute_query", statement="SELECT bad;",
                 dataverse=None, result=ToolResult.error(err))
    entries = audit.recent(10)
    assert len(entries) == 1
    assert entries[0].outcome == "ERROR"
    assert entries[0].error_type == ErrorType.SEMANTIC_ERROR.value
    assert entries[0].error_message == "cannot find dataset"
    assert entries[0].rows_returned is None


def test_record_error_does_not_clobber_existing_entries(settings: Settings) -> None:
    audit = AuditLog(ttl_s=900)
    err = GatewayError(ErrorType.SYNTAX_ERROR, "boom")
    for _ in range(3):
        record_query(audit, settings, tool="execute_query", statement="SELECT;",
                     dataverse=None, result=ToolResult.error(err))
    assert len(audit.recent(10)) == 3  # each error minted a distinct id


# run_get_query_history


async def test_history_empty(settings: Settings) -> None:
    audit = AuditLog(ttl_s=900)
    result = await run_get_query_history(audit, settings)
    assert result.structured["count"] == 0
    assert result.structured["queries"] == []
    assert "0 recent queries" in result.text


async def test_history_newest_first(settings: Settings) -> None:
    audit = AuditLog(ttl_s=900, clock=_stepping_clock())
    for n in range(3):
        record_query(
            audit, settings, tool="execute_query", statement=f"SELECT {n};",
            dataverse=None,
            result=ToolResult(text="ok", structured={"clientContextID": f"c{n}"}),
        )
    result = await run_get_query_history(audit, settings)
    statements = [q["statement"] for q in result.structured["queries"]]
    assert statements == ["SELECT 2;", "SELECT 1;", "SELECT 0;"]


async def test_history_failures_only(settings: Settings) -> None:
    audit = AuditLog(ttl_s=900, clock=_stepping_clock())
    record_query(audit, settings, tool="execute_query", statement="SELECT ok;",
                 dataverse=None,
                 result=ToolResult(text="ok", structured={"clientContextID": "good"}))
    record_query(audit, settings, tool="execute_query", statement="SELECT bad;",
                 dataverse=None,
                 result=ToolResult.error(GatewayError(ErrorType.SYNTAX_ERROR, "nope")))
    result = await run_get_query_history(audit, settings, failures_only=True)
    assert result.structured["count"] == 1
    assert result.structured["queries"][0]["statement"] == "SELECT bad;"
    assert "1 recent failed query" in result.text


async def test_history_limit_clamped(settings: Settings) -> None:
    audit = AuditLog(ttl_s=900, clock=_stepping_clock())
    for n in range(5):
        record_query(audit, settings, tool="execute_query", statement=f"q{n}",
                     dataverse=None,
                     result=ToolResult(text="ok", structured={"clientContextID": f"c{n}"}))
    result = await run_get_query_history(audit, settings, limit=2)
    assert result.structured["count"] == 2
    over = await run_get_query_history(audit, settings, limit=MAX_LIMIT + 50)
    assert over.structured["limit"] == MAX_LIMIT
