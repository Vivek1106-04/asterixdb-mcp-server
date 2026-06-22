"""list_running_queries: the cluster's in-flight requests.

Lists the requests the Cluster Controller is currently running, so an agent can
see what is executing cluster-wide — to spot a long-running query, find the
``clientContextID`` of one to cancel, or confirm a submission is still in flight.
It is the read side of the cancel lifecycle: ``cancel_query`` aborts a request,
this shows which requests exist to abort.

Statement text is redacted by default: the listing reveals that a request is
running and its identifiers without disclosing the query body, which may carry
sensitive literals. Pass ``includeStatements=true`` to include the full text when
the caller needs to identify a specific query.

Defense-in-Depth:
- Layer 1: the schema says statements are redacted unless explicitly requested.
- Layer 2: redaction is requested from the CC itself (``redact=true``), so the
  gateway never has to scrub bodies it received — they are never sent.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..errors import GatewayError
from . import ToolResult


async def run_list_running_queries(
    client: CCClient,
    settings: Settings,
    *,
    include_statements: bool = False,
) -> ToolResult:
    """List the requests the cluster is currently running."""
    try:
        requests = await client.admin_running_requests(redact=not include_statements)
    except GatewayError as err:
        return ToolResult.error(err)

    queries = [req for req in requests if isinstance(req, dict)]
    structured: dict[str, Any] = {
        "status": "success",
        "count": len(queries),
        "statementsRedacted": not include_statements,
        "queries": queries,
    }
    return ToolResult(text=_summarize(len(queries), include_statements), structured=structured)


def _summarize(count: int, include_statements: bool) -> str:
    """One-line human summary for the content text block."""
    if count == 0:
        return "No requests are currently running on the cluster."
    suffix = "" if include_statements else " (statement text redacted)"
    return f"{count} request(s) currently running on the cluster{suffix}."
