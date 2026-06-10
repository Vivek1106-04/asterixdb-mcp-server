"""Unit tests for the async query lifecycle tools."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.audit_log import AuditEntry, AuditLog
from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.permits import PermitPools
from asterixdb_mcp.tools.async_query import (
    run_cancel_query,
    run_fetch_query_result,
    run_submit_async_query,
    run_wait_on_async_query,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog(ttl_s=900)


@pytest.fixture
def pools(settings: Settings) -> PermitPools:
    return PermitPools.from_settings(settings)


# submit_async_query


async def test_submit_records_audit_and_returns_handle(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    envelope = {"status": "running", "handle": "/query/service/status/0-1"}
    cap = make_capturing_cc(settings, response_json=envelope)

    result = await run_submit_async_query(
        cap.client, settings, audit, pools, statement="SELECT * FROM Big LIMIT 5;"
    )

    assert result.is_error is False
    ccid = result.structured["clientContextID"]
    # The status handle is kept internally on the audit entry, not surfaced.
    entry = audit.get(ccid)
    assert entry is not None
    assert entry.handle == "/query/service/status/0-1"


async def test_submit_rejects_invalid_compiler_parameter(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    cap = make_capturing_cc(settings)
    result = await run_submit_async_query(
        cap.client,
        settings,
        audit,
        pools,
        statement="SELECT 1;",
        compiler_parameters={"compiler.notreal": 1},
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value


async def test_submit_forwards_valid_compiler_parameter(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "running", "handle": "h"})
    await run_submit_async_query(
        cap.client,
        settings,
        audit,
        pools,
        statement="SELECT 1;",
        compiler_parameters={"compiler.parallelism": 4},
    )
    assert cap.last_query_form()["compiler.parallelism"] == "4"


async def test_submit_surfaces_compile_error(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    envelope = {"status": "fatal", "errors": [{"code": "ASX1001", "msg": "Syntax error"}]}
    cap = make_capturing_cc(settings, response_json=envelope)
    result = await run_submit_async_query(
        cap.client, settings, audit, pools, statement="SELEKT 1;"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.SYNTAX_ERROR.value


# wait_on_async_query


def _seed_submission(
    audit: AuditLog, ccid: str = "sess-test::_::u", *, handle: str | None = "/status/0-1"
) -> None:
    audit.record(AuditEntry(ccid, "sess", "SELECT 1;", audit.now(), handle=handle))


async def test_wait_unknown_client_context_id_is_not_found(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    cap = make_capturing_cc(settings)
    result = await run_wait_on_async_query(
        cap.client, settings, audit, pools, client_context_id="nope"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.NOT_FOUND.value


async def test_wait_returns_done_on_immediate_success(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    _seed_submission(audit)
    env = {"status": "success", "handle": "/query/service/result/0-1"}
    cap = make_capturing_cc(settings, response_json=env)

    result = await run_wait_on_async_query(
        cap.client, settings, audit, pools, client_context_id="sess-test::_::u"
    )

    assert result.structured["done"] is True
    assert result.structured["clientContextID"] == "sess-test::_::u"
    # The result handle is stashed on the audit entry for fetch to resolve.
    entry = audit.get("sess-test::_::u")
    assert entry is not None
    assert entry.result_handle == "/query/service/result/0-1"


async def test_wait_returns_not_done_on_timeout(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    _seed_submission(audit)
    cap = make_capturing_cc(settings, response_json={"status": "running"})

    # timeout_ms=0 means the deadline is already reached after the first poll.
    result = await run_wait_on_async_query(
        cap.client, settings, audit, pools, client_context_id="sess-test::_::u", timeout_ms=0
    )

    assert result.is_error is False
    assert result.structured["done"] is False
    assert result.structured["queryStatus"] == "running"


async def test_wait_polls_until_success(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    _seed_submission(audit)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(200, json={"status": "running"})
        return httpx.Response(200, json={"status": "success", "handle": "/result/x"})

    cap = make_capturing_cc(settings, handler=handler)
    fake_time = {"t": 0.0}

    async def fake_sleep(_seconds: float) -> None:
        fake_time["t"] += 0.25

    def fake_clock() -> float:
        return fake_time["t"]

    result = await run_wait_on_async_query(
        cap.client,
        settings,
        audit,
        pools,
        client_context_id="sess-test::_::u",
        timeout_ms=10_000,
        sleep=fake_sleep,
        clock=fake_clock,
    )

    assert calls["n"] == 3
    assert result.structured["done"] is True


async def test_wait_success_without_result_handle_skips_stash(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    _seed_submission(audit)
    cap = make_capturing_cc(settings, response_json={"status": "success"})
    result = await run_wait_on_async_query(
        cap.client, settings, audit, pools, client_context_id="sess-test::_::u"
    )
    assert result.structured["done"] is True
    entry = audit.get("sess-test::_::u")
    assert entry is not None
    assert entry.result_handle is None


async def test_wait_surfaces_terminal_failure(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    _seed_submission(audit)
    env = {"status": "failed", "errors": [{"code": "ASX0000", "msg": "runtime boom"}]}
    cap = make_capturing_cc(settings, response_json=env)
    result = await run_wait_on_async_query(
        cap.client, settings, audit, pools, client_context_id="sess-test::_::u"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INTERNAL.value


async def test_wait_maps_timeout_status(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    _seed_submission(audit)
    cap = make_capturing_cc(settings, response_json={"status": "timeout"})
    result = await run_wait_on_async_query(
        cap.client, settings, audit, pools, client_context_id="sess-test::_::u"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.TIMEOUT.value


async def test_wait_failure_without_errors_uses_default_message(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    _seed_submission(audit)
    cap = make_capturing_cc(settings, response_json={"status": "fatal"})
    result = await run_wait_on_async_query(
        cap.client, settings, audit, pools, client_context_id="sess-test::_::u"
    )
    assert result.is_error is True
    assert "fatal" in result.structured["errorMessage"]


async def test_wait_propagates_transport_error(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    _seed_submission(audit)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_wait_on_async_query(
        cap.client, settings, audit, pools, client_context_id="sess-test::_::u"
    )
    assert result.is_error is True


async def test_wait_clamps_timeout_above_ceiling(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    _seed_submission(audit)
    # A request far above max_wait_ms must still complete promptly on success.
    cap = make_capturing_cc(settings, response_json={"status": "success", "handle": "r"})
    result = await run_wait_on_async_query(
        cap.client,
        settings,
        audit,
        pools,
        client_context_id="sess-test::_::u",
        timeout_ms=10_000_000,
    )
    assert result.structured["done"] is True


# fetch_query_result


def _seed_completed(audit: AuditLog, ccid: str = "sess-test::_::u") -> None:
    """Seed an audit entry that has already reached success (result handle set)."""
    entry = AuditEntry(ccid, "sess", "SELECT 1;", audit.now(), handle="/status/0-1")
    audit.record(entry.with_result_handle("/result/x"))


async def test_fetch_unknown_client_context_id_is_not_found(
    settings: Settings, audit: AuditLog
) -> None:
    cap = make_capturing_cc(settings)
    result = await run_fetch_query_result(
        cap.client, settings, audit, client_context_id="nope"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.NOT_FOUND.value


async def test_fetch_before_completion_is_not_ready(
    settings: Settings, audit: AuditLog
) -> None:
    _seed_submission(audit)  # no result handle yet
    cap = make_capturing_cc(settings)
    result = await run_fetch_query_result(
        cap.client, settings, audit, client_context_id="sess-test::_::u"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.NOT_READY.value


async def test_fetch_windows_rows(settings: Settings, audit: AuditLog) -> None:
    _seed_completed(audit)
    rows = [{"i": n} for n in range(10)]
    cap = make_capturing_cc(
        settings, response_json={"status": "success", "results": rows, "metrics": {"x": 1}}
    )

    result = await run_fetch_query_result(
        cap.client, settings, audit, client_context_id="sess-test::_::u", offset=2, limit=3
    )

    assert result.structured["rowsReturned"] == 3
    assert result.structured["results"] == [{"i": 2}, {"i": 3}, {"i": 4}]
    assert result.structured["moreAvailable"] is True
    assert result.structured["metrics"] == {"x": 1}
    # fetch reads the stashed result handle, not the status handle.
    assert cap.requests[-1].url.path == "/result/x"


async def test_fetch_minimizes_output_when_flagged(settings: Settings, audit: AuditLog) -> None:
    from asterixdb_mcp.plan_guard import ADVISORY_TYPE
    from asterixdb_mcp.tools.execute_query import COLUMNAR_FLAGGED_MAX_ROWS

    # A submission flagged at submit time carries the advisory on its audit entry.
    advisory = {"type": ADVISORY_TYPE, "datasets": ["DV.Cols"], "message": "flagged"}
    entry = AuditEntry(
        "sess-test::_::u",
        "sess",
        "SELECT * FROM DV.Cols;",
        audit.now(),
        handle="/status/0-1",
        advisory=advisory,
    )
    audit.record(entry.with_result_handle("/result/x"))
    rows = [{"i": n} for n in range(50)]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})

    result = await run_fetch_query_result(
        cap.client, settings, audit, client_context_id="sess-test::_::u", limit=50
    )

    # Output minimized to the flagged ceiling, advisory re-surfaced, never blocked.
    assert result.structured["rowsReturned"] == COLUMNAR_FLAGGED_MAX_ROWS
    assert result.structured["moreAvailable"] is True
    assert result.structured["advisories"][0]["type"] == ADVISORY_TYPE
    assert "output minimized" in result.text


async def test_fetch_wraps_scalar_result(settings: Settings, audit: AuditLog) -> None:
    _seed_completed(audit)
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": 42})
    result = await run_fetch_query_result(
        cap.client, settings, audit, client_context_id="sess-test::_::u"
    )
    assert result.structured["results"] == [42]


async def test_fetch_propagates_transport_error(
    settings: Settings, audit: AuditLog
) -> None:
    _seed_completed(audit)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_fetch_query_result(
        cap.client, settings, audit, client_context_id="sess-test::_::u"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INTERNAL.value


async def test_fetch_handles_error(settings: Settings, audit: AuditLog) -> None:
    _seed_completed(audit)
    cap = make_capturing_cc(
        settings,
        response_json={"status": "fatal", "errors": [{"code": "ASX1", "msg": "gone"}]},
    )
    result = await run_fetch_query_result(
        cap.client, settings, audit, client_context_id="sess-test::_::u"
    )
    assert result.is_error is True


# cancel_query


async def test_cancel_success_forgets_entry(settings: Settings, audit: AuditLog) -> None:
    audit.record(
        AuditEntry("sess-test::_::u", "sess", "SELECT 1;", audit.now(), handle="h")
    )
    cap = make_capturing_cc(settings, handler=lambda r: httpx.Response(200))

    result = await run_cancel_query(
        cap.client, settings, audit, client_context_id="sess-test::_::u"
    )

    assert result.structured["cancelled"] is True
    assert audit.get("sess-test::_::u") is None


async def test_cancel_not_found_forgets_stale_entry(
    settings: Settings, audit: AuditLog
) -> None:
    audit.record(AuditEntry("sess-test::_::u", "sess", "SELECT 1;", audit.now()))
    cap = make_capturing_cc(settings, handler=lambda r: httpx.Response(404))

    result = await run_cancel_query(
        cap.client, settings, audit, client_context_id="sess-test::_::u"
    )

    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.NOT_FOUND.value
    assert audit.get("sess-test::_::u") is None


async def test_cancel_forbidden_keeps_entry(settings: Settings, audit: AuditLog) -> None:
    audit.record(AuditEntry("sess-test::_::u", "sess", "SELECT 1;", audit.now()))
    cap = make_capturing_cc(settings, handler=lambda r: httpx.Response(403))

    result = await run_cancel_query(
        cap.client, settings, audit, client_context_id="sess-test::_::u"
    )

    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.FORBIDDEN.value
    # A forbidden cancel is not a "gone" signal, so the entry is retained.
    assert audit.get("sess-test::_::u") is not None


async def test_submit_catches_unsupported_function(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    cap = make_capturing_cc(settings)
    result = await run_submit_async_query(
        cap.client, settings, audit, pools, statement="SELECT STDEV(x) FROM y LIMIT 5;"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


# Columnar guard, multi-tenant isolation, egress truncation, signature merge


def _columnar_plan_handler():
    plan = {
        "status": "success",
        "plans": {
            "optimizedLogicalPlan": {
                "operator": "data-scan",
                "data-source": "DV.Cols",
                "inputs": [],
            }
        },
    }
    record = {"DataverseName": "DV", "DatasetName": "Cols", "DatasetFormat": {"Format": "column"}}

    def handler(req: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if form.get("compile-only") == "true":
            return httpx.Response(200, json=plan)
        if "Metadata.`Dataset`" in form.get("statement", ""):
            return httpx.Response(200, json={"status": "success", "results": [record]})
        return httpx.Response(200, json={"status": "running", "handle": "/status/0-1"})

    return handler


async def test_submit_flags_columnar_full_scan_without_blocking(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    from asterixdb_mcp.plan_guard import ADVISORY_TYPE

    cap = make_capturing_cc(settings, handler=_columnar_plan_handler())
    result = await run_submit_async_query(
        cap.client, settings, audit, pools, statement="SELECT * FROM DV.Cols"
    )
    # Not blocked: the submission went through and returned a handle.
    assert result.is_error is False
    assert result.structured["status"] == "submitted"
    # Flagged with a non-fatal advisory, both on the result and on the audit entry.
    assert result.structured["advisories"][0]["type"] == ADVISORY_TYPE
    entry = audit.get(result.structured["clientContextID"])
    assert entry is not None and entry.advisory is not None
    assert entry.advisory["datasets"] == ["DV.Cols"]


async def test_wait_foreign_session_forbidden(
    settings: Settings, audit: AuditLog, pools: PermitPools
) -> None:
    cap = make_capturing_cc(settings)
    result = await run_wait_on_async_query(
        cap.client, settings, audit, pools, client_context_id="other-agent::_::u"
    )
    assert result.structured["errorType"] == ErrorType.FORBIDDEN.value


async def test_fetch_foreign_session_forbidden(settings: Settings, audit: AuditLog) -> None:
    cap = make_capturing_cc(settings)
    result = await run_fetch_query_result(
        cap.client, settings, audit, client_context_id="other-agent::_::u"
    )
    assert result.structured["errorType"] == ErrorType.FORBIDDEN.value


async def test_cancel_foreign_session_forbidden(settings: Settings, audit: AuditLog) -> None:
    cap = make_capturing_cc(settings)
    result = await run_cancel_query(
        cap.client, settings, audit, client_context_id="other-agent::_::u"
    )
    assert result.structured["errorType"] == ErrorType.FORBIDDEN.value
    assert cap.requests == []


async def test_fetch_includes_egress_and_signature(settings: Settings, audit: AuditLog) -> None:
    entry = AuditEntry(
        "sess-test::_::u", "sess-test", "SELECT 1;", audit.now(), handle="/status/0-1"
    )
    entry = entry.with_result_handle("/result/x")
    from dataclasses import replace

    audit.record(replace(entry, signature={"name": ["x"]}))
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": [{"a": 1}]})

    result = await run_fetch_query_result(
        cap.client, settings, audit, client_context_id="sess-test::_::u"
    )
    assert result.structured["egress"]["truncated"] is False
    assert result.structured["signature"] == {"name": ["x"]}
