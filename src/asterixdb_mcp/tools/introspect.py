"""Query introspection tools: validate_syntax, explain_query.

Both compile a statement without running it (``compile-only=true``), so the LLM
can check a query and inspect its plan before paying to execute it.

- ``validate_syntax`` reports whether a statement compiles, and on failure splits
  the cause into SYNTAX (malformed SQL++) vs SEMANTIC (unknown dataset, type
  mismatch). Invalidity is reported as data (``valid: false``), not as a tool
  error, so the model can react to the classification.
- ``explain_query`` returns the optimized logical plan as a structured operator
  tree: operator kinds, the datasets scanned, predicates, and tree depth.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError, classify_cc_error
from ..hint_deriver import hints_payload
from ..plan_parser import parse_optimized_plan
from ..statement_guard import strip_set_prefix
from . import ToolResult


async def run_validate_syntax(
    client: CCClient,
    settings: Settings,
    *,
    statement: str,
    dataverse: str | None = None,
    user_tag: str | None = None,
) -> ToolResult:
    """Compile a statement without running it; report SYNTAX vs SEMANTIC validity."""
    client_context_id = make_client_context_id(settings.agent_session_id, user_tag)
    statement = strip_set_prefix(statement)
    try:
        envelope = await client.compile_query(
            statement, client_context_id=client_context_id, dataverse=dataverse
        )
    except GatewayError as err:
        return ToolResult.error(err)

    error = classify_envelope_error(envelope)
    if error is None:
        structured = {"status": "success", "valid": True}
        return ToolResult(text="Statement compiled successfully (valid).", structured=structured)

    structured = {
        "status": "success",
        "valid": False,
        "errorType": error.error_type.value,
        "errorMessage": error.message,
    }
    if error.asterix_code is not None:
        structured["asterixCode"] = error.asterix_code
    return ToolResult(
        text=f"Statement is invalid ({error.error_type.value}): {error.message}",
        structured=structured,
    )


async def run_explain_query(
    client: CCClient,
    settings: Settings,
    *,
    statement: str,
    dataverse: str | None = None,
    user_tag: str | None = None,
) -> ToolResult:
    """Compile a statement and return its optimized logical plan as a structured tree."""
    client_context_id = make_client_context_id(settings.agent_session_id, user_tag)
    statement = strip_set_prefix(statement)
    try:
        envelope = await client.compile_query(
            statement,
            client_context_id=client_context_id,
            dataverse=dataverse,
            emit_plan=True,
        )
    except GatewayError as err:
        return ToolResult.error(err)

    error = classify_envelope_error(envelope)
    if error is not None:
        # A query that does not compile has no plan to explain.
        return ToolResult.error(error)

    parsed = parse_optimized_plan(envelope.get("plans"))
    if parsed is None:
        return ToolResult.error(
            GatewayError(
                ErrorType.INTERNAL,
                "AsterixDB compiled the statement but returned no optimized plan.",
            )
        )

    structured: dict[str, Any] = {"status": "success", "plan": parsed.to_dict()}

    # Pass the optimizer's own warnings through verbatim — they flag unused hints
    # and cardinality surprises the gateway never re-derives.
    warnings = envelope.get("warnings")
    if warnings:
        structured["warnings"] = warnings

    # Directional hints composed from plan signals (full scan, broadcast join).
    hints = hints_payload(parsed)
    if hints:
        structured["hints"] = hints

    return ToolResult(text=_summarize_plan(parsed, hints), structured=structured)


def _summarize_plan(parsed: Any, hints: list[dict[str, Any]]) -> str:
    """One-line human summary of a parsed plan for the content text block."""
    sources = ", ".join(parsed.data_sources) if parsed.data_sources else "no data source"
    hint_note = f"; {len(hints)} optimization hint(s)" if hints else ""
    return (
        f"Plan depth {parsed.depth} over {sources}; "
        f"{len(parsed.operator_counts)} operator kind(s){hint_note}."
    )


def classify_envelope_error(envelope: dict[str, Any]) -> GatewayError | None:
    """Return a classified error if the compile envelope reports a failure."""
    errors = envelope.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        code = errors[0].get("code")
        code = code if isinstance(code, str) else None
        message = errors[0].get("msg")
        message = message if isinstance(message, str) else "AsterixDB returned an error."
        return classify_cc_error(asterix_code=code, message=message)
    return None
