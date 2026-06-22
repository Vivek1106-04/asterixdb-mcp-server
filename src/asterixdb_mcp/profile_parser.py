"""Compact summary of a Hyracks runtime job profile.

When a query runs with ``profile=timings`` the CC returns a ``profile`` object:
the executed Hyracks job, with per-operator runtime counters spread across every
joblet (node), task, and partition. The raw shape is large and partition-grained;
an agent reasoning about a slow query wants the per-OPERATOR actuals — how long
each operator ran and how many rows it actually produced — not thousands of
partition entries.

This folds the profile into one record per operator (keyed by the operator's
``runtime-id``, which matches the operator ids in the logical/physical plans),
summing run time, output cardinality, and pages read across that operator's
partitions. That is the runtime half of an ``EXPLAIN ANALYZE``: pair it with the
plan from explain_query / explain_physical_plan to see estimated-vs-actual.

Pure transformation — no I/O — so it is unit-tested directly against profile JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Bound the operator list so a wide plan cannot blow the payload; the heaviest
# operators (by run time) are the ones worth seeing.
MAX_OPERATORS = 40


@dataclass
class _OperatorAccumulator:
    """Mutable per-operator running totals folded across partitions."""

    operator_id: str | None
    name: str
    run_time_ms: float = 0.0
    cardinality_out: int = 0
    pages_read: int = 0
    partitions: int = 0

    def add(self, counter: dict[str, Any]) -> None:
        self.run_time_ms += _num(counter.get("run-time"))
        self.cardinality_out += _int(counter.get("cardinality-out"))
        self.pages_read += _int(counter.get("pages-read"))
        self.partitions += 1

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "operator": self.name,
            "runTimeMs": round(self.run_time_ms, 4),
            "cardinalityOut": self.cardinality_out,
            "partitions": self.partitions,
        }
        if self.operator_id is not None:
            out["operatorId"] = self.operator_id
        if self.pages_read:
            out["pagesRead"] = self.pages_read
        return out


@dataclass(frozen=True)
class ProfileSummary:
    """Per-operator runtime actuals distilled from a Hyracks job profile."""

    job_id: str | None
    operators: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "operatorCount": len(self.operators),
            "operators": self.operators,
        }
        if self.job_id is not None:
            out["jobId"] = self.job_id
        return out


def parse_profile(profile: Any) -> ProfileSummary | None:
    """Distil a raw ``profile`` object into per-operator runtime actuals.

    Returns None when ``profile`` is not a usable object, so a caller can omit the
    block rather than surface a malformed one.
    """
    if not isinstance(profile, dict):
        return None
    accumulators: dict[str, _OperatorAccumulator] = {}
    order: list[str] = []
    for counter in _iter_operator_counters(profile):
        name = counter.get("name")
        if not isinstance(name, str):
            continue
        operator_id = counter.get("runtime-id")
        key = operator_id if isinstance(operator_id, str) else name
        acc = accumulators.get(key)
        if acc is None:
            acc = _OperatorAccumulator(
                operator_id=operator_id if isinstance(operator_id, str) else None, name=name
            )
            accumulators[key] = acc
            order.append(key)
        acc.add(counter)
    operators = [accumulators[key].to_dict() for key in order]
    operators.sort(key=lambda op: op["runTimeMs"], reverse=True)
    job_id = profile.get("job-id")
    return ProfileSummary(
        job_id=job_id if isinstance(job_id, str) else None,
        operators=operators[:MAX_OPERATORS],
    )


def _iter_operator_counters(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk joblets -> tasks -> counters and yield every operator counter object."""
    counters: list[dict[str, Any]] = []
    for joblet in _as_list(profile.get("joblets")):
        if not isinstance(joblet, dict):
            continue
        for task in _as_list(joblet.get("tasks")):
            if not isinstance(task, dict):
                continue
            for counter in _as_list(task.get("counters")):
                if isinstance(counter, dict):
                    counters.append(counter)
    return counters


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
