"""Hyracks job (physical plan) JSON parser.

``explain_physical_plan`` asks the CC to compile a statement and emit its
physical **Hyracks job** as JSON (``job=true`` + ``hyracks-job-format=json``).
The CC returns the runtime DAG under ``plans.job`` in this shape (verified against
``JobSpecification.toJSON``)::

    {
      "operators": [
        { "id": "ODID:1", "java-class": "...BTreeSearchOperatorDescriptor",
          "in-arity": 0, "out-arity": 1, "display-name": "...",
          "partition-constraints": {"count": 4, "location": {...}} },
        ...
      ],
      "connectors": [
        { "in-operator-id": "ODID:1", "out-operator-id": "ODID:2",
          "connector": {"id": "...", "java-class": "...MToNPartitioning..."} }
      ]
    }

This module flattens that into an LLM-friendly summary: operator kinds and their
counts, connector kinds (which reveal data movement — broadcast vs hash
repartition), the maximum partition count (parallelism), and the operator/edge
lists. The raw spec is verbose and partition-replicated, so the summary leads and
the bounded detail follows; the gateway never reasons about the plan, it only
surfaces what the CC produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Envelope key holding the physical job DAG, verified against
# ExecutionPlansJsonPrintUtil.JOB_LBL.
JOB_KEY = "job"

_OPERATORS_FIELD = "operators"
_CONNECTORS_FIELD = "connectors"
_ID_FIELD = "id"
_JAVA_CLASS_FIELD = "java-class"
_DISPLAY_NAME_FIELD = "display-name"
_IN_ARITY_FIELD = "in-arity"
_OUT_ARITY_FIELD = "out-arity"
_PARTITION_CONSTRAINTS_FIELD = "partition-constraints"
_COUNT_FIELD = "count"
_CONNECTOR_FIELD = "connector"
_IN_OPERATOR_ID_FIELD = "in-operator-id"
_OUT_OPERATOR_ID_FIELD = "out-operator-id"

# Noise suffix stripped from a Java class name to get a readable operator kind.
_KIND_SUFFIX = "OperatorDescriptor"
_CONNECTOR_KIND_SUFFIX = "ConnectorDescriptor"


@dataclass(frozen=True)
class JobOperator:
    """One physical operator in the Hyracks job DAG."""

    operator_id: str | None
    kind: str | None
    in_arity: int | None
    out_arity: int | None
    partition_count: int | None

    def to_dict(self) -> dict[str, Any]:
        node: dict[str, Any] = {"operatorId": self.operator_id, "kind": self.kind}
        if self.in_arity is not None:
            node["inArity"] = self.in_arity
        if self.out_arity is not None:
            node["outArity"] = self.out_arity
        if self.partition_count is not None:
            node["partitionCount"] = self.partition_count
        return node


@dataclass(frozen=True)
class JobConnector:
    """One physical connector (data-movement edge) between two operators."""

    kind: str | None
    source_operator_id: str | None
    target_operator_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "from": self.source_operator_id,
            "to": self.target_operator_id,
        }


@dataclass(frozen=True)
class ParsedJob:
    """A parsed Hyracks job plus the summaries an LLM reasons over."""

    operators: tuple[JobOperator, ...]
    connectors: tuple[JobConnector, ...]
    operator_counts: dict[str, int]
    connector_counts: dict[str, int]
    max_partition_count: int | None

    def to_dict(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "operatorCount": len(self.operators),
            "connectorCount": len(self.connectors),
            "operatorCounts": self.operator_counts,
            "connectorCounts": self.connector_counts,
            "operators": [op.to_dict() for op in self.operators],
            "connectors": [conn.to_dict() for conn in self.connectors],
        }
        if self.max_partition_count is not None:
            summary["maxPartitionCount"] = self.max_partition_count
        return summary


def parse_job(plans: Any, key: str = JOB_KEY) -> ParsedJob | None:
    """Parse the Hyracks job stored under ``key`` in a CC ``plans`` object.

    Returns None when no recognizable job is present (the statement produced no
    job, the key was not requested, or the CC used the non-JSON ``dot`` format).
    """
    if not isinstance(plans, dict):
        return None
    job = plans.get(key)
    if not isinstance(job, dict):
        return None

    operators = _parse_operators(job.get(_OPERATORS_FIELD))
    connectors = _parse_connectors(job.get(_CONNECTORS_FIELD))
    return ParsedJob(
        operators=operators,
        connectors=connectors,
        operator_counts=_count_kinds(op.kind for op in operators),
        connector_counts=_count_kinds(conn.kind for conn in connectors),
        max_partition_count=_max_partition_count(operators),
    )


def _parse_operators(raw: Any) -> tuple[JobOperator, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(_parse_operator(node) for node in raw if isinstance(node, dict))


def _parse_operator(node: dict[str, Any]) -> JobOperator:
    return JobOperator(
        operator_id=_as_str(node.get(_ID_FIELD)),
        kind=_kind_from_class(node.get(_JAVA_CLASS_FIELD), _KIND_SUFFIX)
        or _as_str(node.get(_DISPLAY_NAME_FIELD)),
        in_arity=_as_int(node.get(_IN_ARITY_FIELD)),
        out_arity=_as_int(node.get(_OUT_ARITY_FIELD)),
        partition_count=_partition_count(node.get(_PARTITION_CONSTRAINTS_FIELD)),
    )


def _parse_connectors(raw: Any) -> tuple[JobConnector, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(_parse_connector(node) for node in raw if isinstance(node, dict))


def _parse_connector(node: dict[str, Any]) -> JobConnector:
    connector = node.get(_CONNECTOR_FIELD)
    java_class = connector.get(_JAVA_CLASS_FIELD) if isinstance(connector, dict) else None
    return JobConnector(
        kind=_kind_from_class(java_class, _CONNECTOR_KIND_SUFFIX),
        source_operator_id=_as_str(node.get(_IN_OPERATOR_ID_FIELD)),
        target_operator_id=_as_str(node.get(_OUT_OPERATOR_ID_FIELD)),
    )


def _partition_count(constraints: Any) -> int | None:
    """Pull the partition count (parallelism) from an operator's constraints."""
    if not isinstance(constraints, dict):
        return None
    return _as_int(constraints.get(_COUNT_FIELD))


def _max_partition_count(operators: tuple[JobOperator, ...]) -> int | None:
    """The widest operator's partition count — the job's peak parallelism."""
    counts = [op.partition_count for op in operators if op.partition_count is not None]
    return max(counts) if counts else None


def _kind_from_class(java_class: Any, suffix: str) -> str | None:
    """Reduce a fully-qualified Java class to a readable kind.

    ``...BTreeSearchOperatorDescriptor`` -> ``BTreeSearch``; the package prefix
    and the ``...Descriptor`` noise suffix are stripped. Returns None for a
    non-string so the caller can fall back to another label.
    """
    if not isinstance(java_class, str) or not java_class:
        return None
    simple = java_class.rsplit(".", 1)[-1]
    if simple.endswith(suffix):
        simple = simple[: -len(suffix)]
    return simple or None


def _count_kinds(kinds: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in kinds:
        if kind is not None:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    # bool is an int subclass; exclude it so a stray boolean is not read as 0/1.
    return value if isinstance(value, int) and not isinstance(value, bool) else None
