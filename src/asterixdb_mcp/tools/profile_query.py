"""profile_query: run a query and return its runtime profile (EXPLAIN ANALYZE).

Where explain_query and explain_physical_plan compile a statement and return the
plan WITHOUT running it (estimates), this RUNS the statement with the engine's
``profile=timings`` mode and returns the actuals: how long each operator ran and
how many rows it really produced, plus the query's execution metrics. Pair it
with explain_query to compare estimated cardinality against measured cardinality
and find where a plan went wrong.

This tool executes the query, so it does real work on the cluster. It stays
read-only (``readonly=true`` is forced like every gateway query) and a ``LIMIT``
is enforced as on execute_query to bound the result the cluster materializes —
the profile therefore reflects the bounded statement. The result ROWS are not
returned (use execute_query for data); only the runtime profile and metrics are.

Defense-in-Depth:
- Layer 1: the schema states this executes the query, is read-only, enforces a
  LIMIT, and returns timings rather than rows.
- Layer 2: the same unsupported-function guard and LIMIT normalization as
  execute_query run before submission; a compile/runtime failure is returned as a
  classified error, never raised.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import GatewayError
from ..profile_parser import parse_profile
from ..statement_guard import check_unsupported_functions, normalize_statement
from . import ToolResult

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


async def run_profile_query(
    client: CCClient,
    settings: Settings,
    *,
    statement: str,
    dataverse: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user_tag: str | None = None,
) -> ToolResult:
    """Execute a read-only query with profiling and return runtime actuals."""
    limit = min(max(limit, 1), MAX_LIMIT)
    bad_function = check_unsupported_functions(statement)
    if bad_function is not None:
        return ToolResult.error(bad_function)
    effective_statement = normalize_statement(statement, limit)
    ccid = make_client_context_id(settings.agent_session_id, user_tag)

    try:
        envelope = await client.execute(
            effective_statement,
            client_context_id=ccid,
            dataverse=dataverse,
            profile=True,
        )
    except GatewayError as err:
        return ToolResult.error(err)

    structured: dict[str, Any] = {
        "status": "success",
        "clientContextID": ccid,
    }
    if effective_statement != statement.strip():
        structured["effectiveStatement"] = effective_statement
    metrics = envelope.get("metrics")
    if metrics is not None:
        structured["metrics"] = metrics
    summary = parse_profile(envelope.get("profile"))
    if summary is not None:
        structured["profile"] = summary.to_dict()
    return ToolResult(text=_summarize(structured), structured=structured)


def _summarize(structured: dict[str, Any]) -> str:
    """One-line human summary naming the heaviest operator, if any."""
    profile = structured.get("profile")
    operators = profile.get("operators") if isinstance(profile, dict) else None
    if not isinstance(profile, dict) or not operators:
        return "Query profiled; no per-operator runtime counters were reported."
    heaviest = operators[0]
    return (
        f"Query profiled: {profile['operatorCount']} operator(s); heaviest is "
        f"{heaviest['operator']} at {heaviest['runTimeMs']} ms "
        f"({heaviest['cardinalityOut']} rows out)."
    )
