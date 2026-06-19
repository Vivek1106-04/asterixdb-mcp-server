"""The explain_physical_plan tool: the Hyracks job (physical runtime plan).

Where ``explain_query`` returns the optimized *logical* plan (what to compute),
this returns the physical **Hyracks job** (how the cluster runs it): the operator
DAG, the connectors that move data between operators (broadcast vs hash
repartition), and the per-operator parallelism. An agent reads it to reason about
data movement and cost before paying to execute.

The job is generated at compile time, so the gateway requests it through the same
read-only ``compile-only=true`` pipeline ``explain_query`` uses — with
``job=true`` — and never executes the statement. The ``EXPLAIN`` keyword is
deliberately NOT prepended: an explain-only statement returns before the job is
generated, so it would only ever yield the logical plan.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError
from ..job_parser import parse_job
from ..statement_guard import strip_set_prefix
from . import ToolResult
from .introspect import classify_envelope_error


async def run_explain_physical_plan(
    client: CCClient,
    settings: Settings,
    *,
    statement: str,
    dataverse: str | None = None,
    user_tag: str | None = None,
) -> ToolResult:
    """Compile a statement and return its physical Hyracks job, summarized for an LLM."""
    client_context_id = make_client_context_id(settings.agent_session_id, user_tag)
    statement = strip_set_prefix(statement)
    try:
        envelope = await client.compile_query(
            statement,
            client_context_id=client_context_id,
            dataverse=dataverse,
            emit_job=True,
        )
    except GatewayError as err:
        return ToolResult.error(err)

    error = classify_envelope_error(envelope)
    if error is not None:
        # A statement that does not compile has no physical job.
        return ToolResult.error(error)

    parsed = parse_job(envelope.get("plans"))
    if parsed is None:
        return ToolResult.error(
            GatewayError(
                ErrorType.INTERNAL,
                "AsterixDB compiled the statement but returned no physical job. "
                "Statements that produce no runtime job (e.g. pure DDL) have no "
                "physical plan to show.",
            )
        )

    structured = {"status": "success", "job": parsed.to_dict()}
    return ToolResult(text=_summarize_job(parsed), structured=structured)


def _summarize_job(parsed: Any) -> str:
    """One-line human summary of a parsed job for the content text block."""
    parallelism = (
        f"up to {parsed.max_partition_count}-way parallel"
        if parsed.max_partition_count is not None
        else "unspecified parallelism"
    )
    return (
        f"Physical job: {len(parsed.operators)} operator(s), "
        f"{len(parsed.connectors)} connector(s), {parallelism}."
    )
