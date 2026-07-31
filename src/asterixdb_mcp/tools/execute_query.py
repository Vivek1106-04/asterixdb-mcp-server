"""execute_query: synchronous, read-only SQL++ execution.

Flow: namespace a clientContextID, forward the statement to the CC with
readonly=true and the egress timeout, then window the returned rows by the
caller's offset/limit. The statement-level LIMIT is the real bound on work done
(egress layer 3); offset/limit here only window the already-bounded result set
for presentation.
"""

from __future__ import annotations

from typing import Any

from ..artifacts import ArtifactFormat, overflow_artifact_payload
from ..cc_client import CCClient
from ..compiler_params import validate_compiler_parameters
from ..config import Settings
from ..context_id import make_client_context_id
from ..egress import COLUMNAR_FLAGGED_MAX_ROWS, bound_rows_for_llm, minimized_caps
from ..errors import ErrorType, GatewayError
from ..plan_guard import ColumnarAdvisory, assess_columnar_scan
from ..statement_guard import check_unsupported_functions, normalize_statement
from ..tenant_policy import check_compiled_plan
from . import ToolResult
from .memory_notes import RecallState, attach_statement_notes

# Mirror the inputSchema bounds so gateway-side windowing stays consistent with
# what the LLM was told it could request.
DEFAULT_LIMIT = 20
MAX_LIMIT = 1000

# Re-exported for tests/readers; the flagged egress ceilings live in egress.py.
__all__ = ["COLUMNAR_FLAGGED_MAX_ROWS", "DEFAULT_LIMIT", "MAX_LIMIT", "run_execute_query"]

# Appended to syntax/semantic failures: the in-gateway reference documents the
# exact error codes and correct SQL++ patterns, but models rarely consult it
# unprompted — the error is the moment they will.
REFERENCE_HINT = (
    "Consult get_reference('errors') for this error code and "
    "get_reference('queries') for correct SQL++ patterns."
)
_HINTED_ERRORS = frozenset({ErrorType.SYNTAX_ERROR, ErrorType.SEMANTIC_ERROR})


def _with_reference_hint(err: GatewayError) -> GatewayError:
    """Return the error with the reference pointer appended when it can help."""
    if err.error_type not in _HINTED_ERRORS:
        return err
    return GatewayError(
        err.error_type,
        err.message + " " + REFERENCE_HINT,
        asterix_code=err.asterix_code,
    )


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
    download_format: ArtifactFormat | None = None,
    recall: RecallState | None = None,
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
        plan_envelope = await client.compile_query(
            effective_statement,
            client_context_id=client_context_id,
            dataverse=dataverse,
            emit_plan=True,
        )
        # Authorize before executing, off the plan we already had to compile.
        check_compiled_plan(settings, client.principal, plan_envelope, dataverse)
        advisory = await assess_columnar_scan(
            client, client_context_id, plan_envelope.get("plans"), dataverse
        )
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
        # A failed query rides back with the learned notes for the datasets it
        # referenced — often the note IS the fix (a field gotcha, a proven
        # pattern) and the model never had to ask for it.
        return await attach_statement_notes(
            client,
            client_context_id,
            statement,
            ToolResult.error(_with_reference_hint(err)),
            recall=recall,
            settings=settings,
            dataverse=dataverse,
        )

    rows = envelope.get("results") or []
    if not isinstance(rows, list):
        rows = [rows]
    paged = rows[offset : offset + limit]
    more_available = offset + limit < len(rows)
    # Egress layer 4: cap what actually reaches the LLM. A flagged columnar full
    # scan tightens these caps further to minimize output (the query still ran).
    max_rows, max_bytes = _egress_caps(settings, advisory)
    window, truncation = bound_rows_for_llm(paged, max_rows, max_bytes, settings.max_field_chars)

    # When the LLM did not see the whole result (rows beyond this page, or rows
    # dropped by the context-window cap), persist the full set to a downloadable
    # file and reference it instead of discarding the overflow.
    artifact = overflow_artifact_payload(
        rows,
        overflow=more_available or truncation["truncated"],
        settings=settings,
        fmt=download_format,
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
    if advisory is not None:
        structured["advisories"] = [advisory.to_payload()]

    result = ToolResult(text=_summarize(structured), structured=structured)
    # Ambient recall: the first successful query touching a dataset this session
    # carries its learned notes, so recall never depends on the model asking.
    return await attach_statement_notes(
        client,
        client_context_id,
        statement,
        result,
        recall=recall,
        first_use_only=True,
        settings=settings,
        dataverse=dataverse,
    )


def _egress_caps(settings: Settings, advisory: ColumnarAdvisory | None) -> tuple[int, int]:
    """Row/byte egress caps, tightened when a columnar full scan was flagged."""
    if advisory is None:
        return settings.max_rows_to_llm, settings.max_bytes_to_llm
    return minimized_caps(settings.max_rows_to_llm, settings.max_bytes_to_llm)


def _summarize(structured: dict[str, Any]) -> str:
    """One-line human summary for the ``content`` text block."""
    parts = [f"Returned {structured['rowsReturned']} row(s)"]
    if structured["offset"]:
        parts.append(f"from offset {structured['offset']}")
    if structured["moreAvailable"]:
        parts.append("(more rows available in this result, increase limit or page with offset)")
    if structured.get("advisories"):
        parts.append(
            "[columnar full scan flagged — output minimized; project columns or add a "
            "WHERE filter to widen]"
        )
    return " ".join(parts) + "."
