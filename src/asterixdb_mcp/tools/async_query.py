"""Async query lifecycle tools: submit, wait, fetch, cancel.

Long-running queries become first-class. Instead of a blocking call that holds a
connection open, the LLM:

1. ``submit_async_query`` -> gets a ``clientContextID`` and a status ``handle``,
2. ``wait_on_async_query`` -> long-polls that handle for up to a bounded window,
3. ``fetch_query_result`` -> streams the rows once the status is success,
4. ``cancel_query`` -> aborts a still-running query by its ``clientContextID``.

The gateway holds no CC state: handles and clientContextIDs are the CC's own
identifiers. A small TTL-bounded audit log remembers each submission so cancel
can be keyed on the clientContextID and a session can recover a lost handle.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..artifacts import ArtifactFormat, overflow_artifact_payload
from ..audit_log import OUTCOME_SUBMITTED, AuditEntry, AuditLog
from ..cc_client import HANDLE_FIELD, STATUS_FIELD, CCClient
from ..compiler_params import validate_compiler_parameters
from ..config import Settings
from ..context_id import make_client_context_id, parse_client_context_id, sanitize_segment
from ..egress import bound_rows_for_llm, minimized_caps
from ..errors import ErrorType, GatewayError, classify_cc_error
from ..permits import PermitPools
from ..plan_guard import assess_columnar_scan
from ..statement_guard import check_unsupported_functions, normalize_statement
from . import ToolResult

# Result windowing bounds, mirrored from the inputSchema.
DEFAULT_LIMIT = 20
MAX_LIMIT = 1000

# CC result statuses that are terminal (no further polling will change them).
_TERMINAL_STATUSES = frozenset({"success", "failed", "fatal", "timeout"})
_SUCCESS_STATUS = "success"

# Statuses that mean "the query failed" (terminal but not success).
_FAILURE_ERROR_TYPES: dict[str, ErrorType] = {
    "timeout": ErrorType.TIMEOUT,
    "failed": ErrorType.INTERNAL,
    "fatal": ErrorType.INTERNAL,
}


async def run_submit_async_query(
    client: CCClient,
    settings: Settings,
    audit: AuditLog,
    pools: PermitPools,
    *,
    statement: str,
    dataverse: str | None = None,
    compiler_parameters: dict[str, Any] | None = None,
    user_tag: str | None = None,
) -> ToolResult:
    """Submit a read-only query for async execution; return its handle."""
    bad_function = check_unsupported_functions(statement)
    if bad_function is not None:
        return ToolResult.error(bad_function)

    try:
        validated = (
            validate_compiler_parameters(compiler_parameters) if compiler_parameters else None
        )
    except GatewayError as err:
        return ToolResult.error(err)

    effective_statement = normalize_statement(statement, MAX_LIMIT)
    client_context_id = make_client_context_id(settings.agent_session_id, user_tag)
    try:
        # One compile-only call does double duty: it gives the plan for the
        # columnar guardrail and the result signature to merge into fetch (the
        # async result cache strips the envelope, so the signature is captured now).
        compiled = await client.compile_query(
            effective_statement,
            client_context_id=client_context_id,
            dataverse=dataverse,
            emit_plan=True,
            signature=True,
        )
        advisory = await assess_columnar_scan(
            client, client_context_id, compiled.get("plans"), dataverse
        )
        async with pools.async_.acquire():
            envelope = await client.submit_async(
                effective_statement,
                client_context_id=client_context_id,
                dataverse=dataverse,
                compiler_parameters=validated,
            )
    except GatewayError as err:
        return ToolResult.error(err)

    handle = _as_str(envelope.get(HANDLE_FIELD))
    status = _as_str(envelope.get(STATUS_FIELD)) or "running"
    advisory_payload = advisory.to_payload() if advisory is not None else None
    audit.record(
        AuditEntry(
            client_context_id=client_context_id,
            session=settings.agent_session_id,
            statement=statement,
            submitted_at=audit.now(),
            handle=handle,
            dataverse=dataverse,
            signature=compiled.get("signature"),
            advisory=advisory_payload,
            tool="submit_async_query",
            outcome=OUTCOME_SUBMITTED,
        )
    )

    structured: dict[str, Any] = {
        "status": "submitted",
        "clientContextID": client_context_id,
        "queryStatus": status,
    }
    text = (
        f"Submitted async query. Pass clientContextID {client_context_id!r} to "
        f"wait_on_async_query, then fetch_query_result."
    )
    if advisory_payload is not None:
        structured["advisories"] = [advisory_payload]
        text += " Note: columnar full scan flagged — fetched output will be minimized."
    return ToolResult(text=text, structured=structured)


async def run_wait_on_async_query(
    client: CCClient,
    settings: Settings,
    audit: AuditLog,
    pools: PermitPools,
    *,
    client_context_id: str,
    timeout_ms: int | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] | None = None,
) -> ToolResult:
    """Long-poll a submitted query (by clientContextID) until terminal or window ends.

    The handle is resolved from the audit log, so the caller only ever carries the
    clientContextID returned by submit_async_query. The wait is bounded
    (``timeout_ms`` clamped to ``max_wait_ms``) and polls at the configured
    cadence. If the window elapses before the query finishes, the tool returns
    ``done=false`` so the caller can wait again, rather than holding the
    connection open indefinitely.
    """
    owned = _check_ownership(settings, client_context_id)
    if owned is not None:
        return ToolResult.error(owned)
    entry = audit.get(client_context_id)
    if entry is None or entry.handle is None:
        return ToolResult.error(_unknown_submission_error(client_context_id))

    sleep = sleep or asyncio.sleep
    clock = clock or time.monotonic
    budget_ms = _clamp_timeout(timeout_ms, settings.max_wait_ms)
    interval_s = settings.wait_poll_interval_ms / 1000.0
    deadline = clock() + budget_ms / 1000.0

    handle = entry.handle
    try:
        async with pools.waits.acquire():
            return await _poll_until_terminal(
                client, audit, entry, handle, deadline, interval_s, sleep, clock
            )
    except GatewayError as err:
        return ToolResult.error(err)


async def _poll_until_terminal(
    client: CCClient,
    audit: AuditLog,
    entry: AuditEntry,
    handle: str,
    deadline: float,
    interval_s: float,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
) -> ToolResult:
    """Poll the status handle until terminal or the deadline passes."""
    while True:
        envelope = await client.poll_status(handle)
        status = (_as_str(envelope.get(STATUS_FIELD)) or "").lower()
        if status in _TERMINAL_STATUSES:
            return _terminal_result(audit, entry, envelope, status)
        if clock() >= deadline:
            return _still_running_result(entry.client_context_id, status)
        await sleep(interval_s)


def _terminal_result(
    audit: AuditLog, entry: AuditEntry, envelope: dict[str, Any], status: str
) -> ToolResult:
    """Build the result for a query that has reached a terminal status."""
    if status == _SUCCESS_STATUS:
        # Stash the result handle so fetch_query_result can resolve it from the
        # same clientContextID; the caller never has to carry a second id.
        result_handle = _as_str(envelope.get(HANDLE_FIELD))
        if result_handle is not None:
            audit.record(entry.with_result_handle(result_handle))
        structured = {
            "status": "success",
            "done": True,
            "queryStatus": status,
            "clientContextID": entry.client_context_id,
        }
        return ToolResult(
            text=(
                "Query finished. Fetch rows with fetch_query_result using clientContextID "
                f"{entry.client_context_id!r}."
            ),
            structured=structured,
        )
    error_type = _FAILURE_ERROR_TYPES.get(status, ErrorType.INTERNAL)
    message = _first_error_message(envelope) or f"Async query ended with status {status!r}."
    err = GatewayError(error_type, message)
    return ToolResult.error(err)


def _still_running_result(client_context_id: str, status: str) -> ToolResult:
    """Build the result for a query that is still running past the wait window."""
    structured = {
        "status": "pending",
        "done": False,
        "queryStatus": status or "running",
        "clientContextID": client_context_id,
    }
    return ToolResult(
        text="Query is still running. Call wait_on_async_query again to keep waiting.",
        structured=structured,
    )


async def run_fetch_query_result(
    client: CCClient,
    settings: Settings,
    audit: AuditLog,
    *,
    client_context_id: str,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    download_format: ArtifactFormat | None = None,
) -> ToolResult:
    """Fetch and window the rows of a completed async query by its clientContextID."""
    owned = _check_ownership(settings, client_context_id)
    if owned is not None:
        return ToolResult.error(owned)
    entry = audit.get(client_context_id)
    if entry is None:
        return ToolResult.error(_unknown_submission_error(client_context_id))
    if entry.result_handle is None:
        return ToolResult.error(
            GatewayError(
                ErrorType.NOT_READY,
                f"Query {client_context_id} has no result yet. Call wait_on_async_query "
                "until it reports done:true before fetching.",
            )
        )

    offset = max(offset, 0)
    limit = min(max(limit, 1), MAX_LIMIT)
    try:
        envelope = await client.fetch_result(entry.result_handle)
    except GatewayError as err:
        return ToolResult.error(err)

    # A GET on the result handle returns errors inline rather than via HTTP
    # status; classify and surface them instead of reporting zero rows.
    error = _classify_envelope_error(envelope)
    if error is not None:
        return ToolResult.error(error)

    rows = envelope.get("results") or []
    if not isinstance(rows, list):
        rows = [rows]
    paged = rows[offset : offset + limit]
    # Egress layer 4: clamp huge field values, then cap what reaches the LLM. A
    # query flagged at submission as a columnar full scan tightens the caps so the
    # fetched output is minimized (the query still ran).
    max_rows, max_bytes = settings.max_rows_to_llm, settings.max_bytes_to_llm
    if entry.advisory is not None:
        max_rows, max_bytes = minimized_caps(max_rows, max_bytes)
    window, truncation = bound_rows_for_llm(
        paged, max_rows, max_bytes, settings.max_field_chars
    )
    more_available = (offset + limit < len(rows)) or truncation["truncated"]
    # Persist the full result for download when the fetched window did not deliver
    # everything (more pages, or rows trimmed for the context window).
    artifact = overflow_artifact_payload(
        rows, overflow=more_available, settings=settings, fmt=download_format
    )
    if artifact is not None:
        truncation["artifact"] = artifact
    structured: dict[str, Any] = {
        "status": "success",
        "clientContextID": client_context_id,
        "rowsReturned": len(window),
        "rowsAvailableInResponse": len(rows),
        "offset": offset,
        "limit": limit,
        "moreAvailable": more_available,
        "results": window,
        "egress": truncation,
    }
    metrics = envelope.get("metrics")
    if metrics is not None:
        structured["metrics"] = metrics
    # Signature merge: the async result cache strips the envelope, so surface the
    # signature captured at submission time.
    if entry.signature is not None:
        structured["signature"] = entry.signature
    text = f"Returned {len(window)} row(s) from the async result."
    if entry.advisory is not None:
        structured["advisories"] = [entry.advisory]
        text += " [columnar full scan flagged — output minimized]"
    return ToolResult(text=text, structured=structured)


async def run_cancel_query(
    client: CCClient,
    settings: Settings,
    audit: AuditLog,
    *,
    client_context_id: str,
) -> ToolResult:
    """Cancel a running query by its clientContextID and forget its audit entry."""
    owned = _check_ownership(settings, client_context_id)
    if owned is not None:
        return ToolResult.error(owned)
    try:
        cancelled = await client.cancel(client_context_id)
    except GatewayError as err:
        # A NOT_FOUND means the query already finished; drop any stale audit entry.
        if err.error_type is ErrorType.NOT_FOUND:
            audit.forget(client_context_id)
        return ToolResult.error(err)

    audit.forget(client_context_id)
    structured = {
        "status": "cancelled",
        "clientContextID": client_context_id,
        "cancelled": cancelled,
    }
    return ToolResult(text=f"Cancelled query {client_context_id}.", structured=structured)


def _check_ownership(settings: Settings, client_context_id: str) -> GatewayError | None:
    """Block a clientContextID minted by a different gateway session (multi-tenant).

    The session segment of the namespaced clientContextID must match this
    gateway's session, so one agent cannot wait on, fetch, or cancel another
    agent's query by guessing or replaying its id. A malformed id is left to the
    audit-miss NOT_FOUND path.
    """
    try:
        session, _, _ = parse_client_context_id(client_context_id)
    except ValueError:
        return None
    if session != sanitize_segment(settings.agent_session_id):
        return GatewayError(
            ErrorType.FORBIDDEN,
            "This clientContextID belongs to a different session. You can only wait on, "
            "fetch, or cancel queries you submitted in this session.",
        )
    return None


def _unknown_submission_error(client_context_id: str) -> GatewayError:
    """Error for a clientContextID with no live audit entry (unknown/expired)."""
    return GatewayError(
        ErrorType.NOT_FOUND,
        f"No submitted query found for clientContextID {client_context_id!r}. Submit one with "
        "submit_async_query first, and pass back the exact clientContextID it returned.",
    )


def _clamp_timeout(requested_ms: int | None, ceiling_ms: int) -> int:
    """Clamp a requested wait window to [0, ceiling]; default to the ceiling."""
    if requested_ms is None:
        return ceiling_ms
    return min(max(requested_ms, 0), ceiling_ms)


def _first_error_message(envelope: dict[str, Any]) -> str | None:
    errors = envelope.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return _as_str(errors[0].get("msg"))
    return None


def _classify_envelope_error(envelope: dict[str, Any]) -> GatewayError | None:
    """Return a classified error if the envelope carries an ``errors`` list."""
    errors = envelope.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        code = _as_str(errors[0].get("code"))
        message = _as_str(errors[0].get("msg")) or "AsterixDB returned an error."
        return classify_cc_error(asterix_code=code, message=message)
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
