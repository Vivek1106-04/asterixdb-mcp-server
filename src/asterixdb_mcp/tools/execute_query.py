"""execute_query: synchronous, read-only SQL++ execution.

Flow: namespace a clientContextID, forward the statement to the CC with
readonly=true and the egress timeout, then window the returned rows by the
caller's offset/limit. The statement-level LIMIT is the real bound on work done
(egress layer 3); offset/limit here only window the already-bounded result set
for presentation.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..compiler_params import validate_compiler_parameters
from ..config import Settings
from ..context_id import make_client_context_id
from ..egress import bound_rows_for_llm
from ..errors import GatewayError
from ..plan_guard import enforce_columnar_safety
from ..statement_guard import check_unsupported_functions, normalize_statement
from . import ToolResult

# Mirror the inputSchema bounds so gateway-side windowing stays consistent with
# what the LLM was told it could request.
DEFAULT_LIMIT = 20
MAX_LIMIT = 1000


async def run_execute_query(
    client: CCClient,
    settings: Settings,
    *,
    statement: str,
    dataverse: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    compiler_parameters: dict[str, Any] | None = None,
    profile: bool = False,
    signature: bool = False,
    max_warnings: int = 5,
    user_tag: str | None = None,
) -> ToolResult:
    """Execute a read-only SQL++ query and return a windowed result envelope."""
    offset = max(offset, 0)
    limit = min(max(limit, 1), MAX_LIMIT)
    client_context_id = make_client_context_id(settings.agent_session_id, user_tag)

    bad_function = check_unsupported_functions(statement)
    if bad_function is not None:
        return ToolResult.error(bad_function)
    effective_statement = normalize_statement(statement, limit)

    try:
        validated_params = (
            validate_compiler_parameters(compiler_parameters) if compiler_parameters else None
        )
        rejection = await _columnar_preflight(
            client, client_context_id, effective_statement, dataverse
        )
        if rejection is not None:
            return ToolResult.error(rejection)
        envelope = await client.execute(
            effective_statement,
            client_context_id=client_context_id,
            dataverse=dataverse,
            signature=signature,
            profile=profile,
            max_warnings=max_warnings,
            compiler_parameters=validated_params,
        )
    except GatewayError as err:
        return ToolResult.error(err)

    rows = envelope.get("results") or []
    if not isinstance(rows, list):
        rows = [rows]
    paged = rows[offset : offset + limit]
    more_available = offset + limit < len(rows)
    # Egress layer 4: cap what actually reaches the LLM.
    window, truncation = bound_rows_for_llm(
        paged, settings.max_rows_to_llm, settings.max_bytes_to_llm, settings.max_field_chars
    )

    structured: dict[str, Any] = {
        "status": "success",
        "clientContextID": client_context_id,
        "rowsReturned": len(window),
        "rowsAvailableInResponse": len(rows),
        "offset": offset,
        "limit": limit,
        "moreAvailable": more_available or truncation["truncated"],
        "results": window,
        "egress": truncation,
    }
    if effective_statement != statement.strip():
        structured["effectiveStatement"] = effective_statement
    metrics = envelope.get("metrics")
    if metrics is not None:
        structured["metrics"] = metrics
    if signature and envelope.get("signature") is not None:
        structured["signature"] = envelope["signature"]
    warnings = envelope.get("warnings")
    if warnings:
        structured["warnings"] = warnings

    return ToolResult(text=_summarize(structured), structured=structured)


async def _columnar_preflight(
    client: CCClient, ccid: str, statement: str, dataverse: str | None
) -> GatewayError | None:
    """Compile-only the statement and reject an unrestricted columnar full scan.

    A compile failure here yields no plan (no rejection); the subsequent real
    execute surfaces the actual compile error to the caller.
    """
    plan_env = await client.compile_query(
        statement, client_context_id=ccid, dataverse=dataverse, emit_plan=True
    )
    return await enforce_columnar_safety(client, ccid, plan_env.get("plans"), dataverse)


def _summarize(structured: dict[str, Any]) -> str:
    """One-line human summary for the ``content`` text block."""
    parts = [f"Returned {structured['rowsReturned']} row(s)"]
    if structured["offset"]:
        parts.append(f"from offset {structured['offset']}")
    if structured["moreAvailable"]:
        parts.append("(more rows available in this result, increase limit or page with offset)")
    return " ".join(parts) + "."
