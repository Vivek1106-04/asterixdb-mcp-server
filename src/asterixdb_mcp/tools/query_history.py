"""get_query_history: recent query activity for agent self-debugging.

A read of the gateway's in-memory audit log, projected to "what was run and how
it turned out." The intended consumer is the agent itself: after a query fails,
it can recall the exact statement and the classified error instead of re-deriving
them, and it can see whether a previous formulation already succeeded.

This reaches no cluster — it reads gateway memory only. Entries are this
session's submissions, TTL-bounded by the same window as the async audit log, so
the view is naturally scoped and self-pruning. `record_query` is the single point
that stamps a tool call's outcome into that log.
"""

from __future__ import annotations

from typing import Any

from ..audit_log import (
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    AuditEntry,
    AuditLog,
)
from ..config import Settings
from ..context_id import make_client_context_id
from . import ToolResult

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


async def run_get_query_history(
    audit: AuditLog,
    settings: Settings,
    *,
    limit: int = DEFAULT_LIMIT,
    failures_only: bool = False,
) -> ToolResult:
    """Return the most recent recorded queries, newest first."""
    limit = min(max(limit, 1), MAX_LIMIT)
    entries = audit.recent(limit, failures_only=failures_only)
    queries = [e.to_history_view() for e in entries]
    structured: dict[str, Any] = {
        "status": "success",
        "limit": limit,
        "failuresOnly": failures_only,
        "count": len(queries),
        "queries": queries,
    }
    noun = "query" if len(queries) == 1 else "queries"
    kind = "failed " if failures_only else ""
    text = f"{len(queries)} recent {kind}{noun} in this session."
    return ToolResult(text=text, structured=structured)


def record_query(
    audit: AuditLog,
    settings: Settings,
    *,
    tool: str,
    statement: str,
    dataverse: str | None,
    result: ToolResult,
) -> None:
    """Stamp a completed tool call's outcome into the audit log.

    Called from the server adapter after a query tool returns. A successful
    result carries its clientContextID, which keys the entry; an error envelope
    has none, so a fresh id is minted to avoid clobbering an unrelated entry.
    """
    structured = result.structured or {}
    ccid = structured.get("clientContextID") or make_client_context_id(
        settings.agent_session_id, tool
    )
    if result.is_error:
        outcome = OUTCOME_ERROR
        error_type = structured.get("errorType")
        error_message = structured.get("errorMessage")
        rows_returned = None
    else:
        outcome = OUTCOME_SUCCESS
        error_type = None
        error_message = None
        rows_returned = structured.get("rowsReturned")
    audit.record(
        AuditEntry(
            client_context_id=ccid,
            session=settings.agent_session_id,
            statement=statement,
            submitted_at=audit.now(),
            dataverse=dataverse,
            tool=tool,
            outcome=outcome,
            error_type=error_type,
            error_message=error_message,
            rows_returned=rows_returned,
        )
    )
