"""Directional optimization hints derived from a parsed optimized plan.

Section 12.4: the gateway composes *direction* from cluster-authored facts (the
plan the optimizer produced); it never re-plans or rewrites the user's SQL++.
Each hint names the plan signal it saw and the SQL++ lever that addresses it, so
the agent can decide whether to push a predicate, add an index, or restructure a
join. The agent writes the actual hint syntax using the ``query-hints``
reference topic; the gateway only points the way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .plan_parser import ParsedPlan, PlanOperator

# Optimizer signals we read off the optimized logical plan.
_DATA_SCAN = "data-scan"
_BROADCAST = "BROADCAST"


@dataclass(frozen=True)
class Hint:
    """One piece of directional advice: the signal seen and the lever to pull."""

    code: str
    signal: str
    advice: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "signal": self.signal, "advice": self.advice}


def derive_hints(parsed: ParsedPlan) -> list[Hint]:
    """Turn plan signals into a directional hint set; empty when nothing stands out."""
    hints: list[Hint] = []
    if _DATA_SCAN in parsed.operator_counts:
        hints.append(
            Hint(
                code="full-scan",
                signal="The plan scans a dataset without an index (data-scan).",
                advice=(
                    "Add a selective WHERE predicate or a secondary index; confirm with "
                    "check_index_usage or recommend_indexes."
                ),
            )
        )
    if _has_broadcast(parsed.root):
        hints.append(
            Hint(
                code="broadcast-join",
                signal="A join input is broadcast to every partition.",
                advice=(
                    "Ensure the broadcast side is the smaller input; the hash-bcast and "
                    "indexnl SQL++ hints steer join strategy (see the query-hints topic)."
                ),
            )
        )
    return hints


def hints_payload(parsed: ParsedPlan) -> list[dict[str, Any]]:
    """Serialize derived hints to JSON-friendly dicts for a tool payload."""
    return [hint.to_dict() for hint in derive_hints(parsed)]


def _has_broadcast(op: PlanOperator) -> bool:
    """True when any operator in the tree uses a broadcast exchange."""
    if op.physical_operator is not None and _BROADCAST in op.physical_operator.upper():
        return True
    return any(_has_broadcast(child) for child in op.inputs)
