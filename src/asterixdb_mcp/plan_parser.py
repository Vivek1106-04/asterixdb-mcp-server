"""Optimized-logical-plan JSON parser.

``explain_query`` asks the CC to compile a statement and emit its optimized
logical plan as JSON (``plan-format=clean_json``). The CC returns an operator
tree under ``plans.optimizedLogicalPlan`` where each node looks like::

    {
      "operator": "data-scan",
      "operatorId": "1.2",
      "physical-operator": "...",
      "data-source": "Yelp.Business",   # scans only
      "condition": {"expressions": [...]},  # select/join only
      "inputs": [ { ...child node... } ]
    }

This module turns that nested JSON into a flattened, LLM-friendly summary:
operator kinds, the data sources touched, the predicates applied, and the tree
depth. It is deliberately tolerant of missing or unexpected fields — the CC is
the authority on the plan shape; the gateway only surfaces what it recognizes.

The same parser is reused by ``check_index_usage`` and the columnar
plan-rejection guardrail, so it stays free of any explain-specific presentation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Envelope key holding the optimized operator tree, verified against
# ExecutionPlansJsonPrintUtil.OPTIMIZED_LOGICAL_PLAN_LBL.
OPTIMIZED_PLAN_KEY = "optimizedLogicalPlan"

# Envelope key for the *unoptimized* logical plan (requested with
# logical-plan=true). The optimizer rewrites declared-field access to
# field-access-by-INDEX — the field name is lost — so a tool that needs the
# field NAME must read this plan, where access is still by name.
UNOPTIMIZED_PLAN_KEY = "logicalPlan"

_OPERATOR_FIELD = "operator"
_OPERATOR_ID_FIELD = "operatorId"
_PHYSICAL_OPERATOR_FIELD = "physical-operator"
_DATA_SOURCE_FIELD = "data-source"
_CONDITION_FIELD = "condition"
_EXPRESSIONS_FIELD = "expressions"
_INPUTS_FIELD = "inputs"


@dataclass(frozen=True)
class PlanOperator:
    """One node in the parsed operator tree."""

    kind: str | None
    operator_id: str | None
    physical_operator: str | None
    data_source: str | None
    predicates: tuple[str, ...]
    inputs: tuple[PlanOperator, ...]

    def to_dict(self) -> dict[str, Any]:
        """Render to a JSON-friendly dict, omitting empty optional fields."""
        node: dict[str, Any] = {"operator": self.kind}
        if self.operator_id is not None:
            node["operatorId"] = self.operator_id
        if self.physical_operator is not None:
            node["physicalOperator"] = self.physical_operator
        if self.data_source is not None:
            node["dataSource"] = self.data_source
        if self.predicates:
            node["predicates"] = list(self.predicates)
        if self.inputs:
            node["inputs"] = [child.to_dict() for child in self.inputs]
        return node


@dataclass(frozen=True)
class ParsedPlan:
    """A parsed plan plus derived summaries useful to downstream tools."""

    root: PlanOperator
    operator_counts: dict[str, int]
    data_sources: tuple[str, ...]
    depth: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "operatorCounts": self.operator_counts,
            "dataSources": list(self.data_sources),
            "depth": self.depth,
            "tree": self.root.to_dict(),
        }


def parse_plan(plans: Any, key: str) -> ParsedPlan | None:
    """Parse the operator tree stored under ``key`` in a CC ``plans`` object.

    Returns None when no recognizable plan tree is present under that key (e.g.
    the statement produced no plan, the key was not requested, or the CC used a
    non-JSON plan format).
    """
    if not isinstance(plans, dict):
        return None
    root_node = plans.get(key)
    if not isinstance(root_node, dict):
        return None

    root = _parse_node(root_node)
    counts: dict[str, int] = {}
    sources: list[str] = []
    _collect(root, counts, sources)
    return ParsedPlan(
        root=root,
        operator_counts=counts,
        data_sources=tuple(_dedupe_preserving_order(sources)),
        depth=_depth(root),
    )


def parse_optimized_plan(plans: Any) -> ParsedPlan | None:
    """Parse the optimized logical plan out of a CC ``plans`` object."""
    return parse_plan(plans, OPTIMIZED_PLAN_KEY)


def datasets_from_sources(
    data_sources: tuple[str, ...], default_dataverse: str | None
) -> list[tuple[str, str]]:
    """Turn plan data-source strings (``Dataverse.Dataset[.index]``) into (dv, ds) pairs.

    A single-segment source (just a dataset name) is qualified with
    ``default_dataverse`` when one is given, otherwise skipped. Order-preserving
    and de-duplicated.
    """
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for source in data_sources:
        parts = source.split(".")
        if len(parts) >= 2:
            pair = (parts[0], parts[1])
        elif default_dataverse:
            pair = (default_dataverse, parts[0])
        else:
            continue
        if pair not in seen:
            seen.add(pair)
            result.append(pair)
    return result


def _parse_node(node: dict[str, Any]) -> PlanOperator:
    """Recursively parse one operator node and its inputs."""
    raw_inputs = node.get(_INPUTS_FIELD)
    children = (
        tuple(_parse_node(child) for child in raw_inputs if isinstance(child, dict))
        if isinstance(raw_inputs, list)
        else ()
    )
    return PlanOperator(
        kind=_as_str(node.get(_OPERATOR_FIELD)),
        operator_id=_as_str(node.get(_OPERATOR_ID_FIELD)),
        physical_operator=_as_str(node.get(_PHYSICAL_OPERATOR_FIELD)),
        data_source=_as_str(node.get(_DATA_SOURCE_FIELD)),
        predicates=_extract_predicates(node),
        inputs=children,
    )


def _extract_predicates(node: dict[str, Any]) -> tuple[str, ...]:
    """Collect predicate/expression strings from a node's condition/expressions.

    A condition is emitted as ``{"expressions": [...]}``; some operators also
    carry a top-level ``expressions`` array. Both are flattened to strings.
    """
    predicates: list[str] = []
    condition = node.get(_CONDITION_FIELD)
    if isinstance(condition, dict):
        predicates.extend(_string_list(condition.get(_EXPRESSIONS_FIELD)))
    elif isinstance(condition, str):
        predicates.append(condition)
    predicates.extend(_string_list(node.get(_EXPRESSIONS_FIELD)))
    return tuple(predicates)


def _collect(op: PlanOperator, counts: dict[str, int], sources: list[str]) -> None:
    """Walk the tree accumulating operator counts and data sources."""
    if op.kind is not None:
        counts[op.kind] = counts.get(op.kind, 0) + 1
    if op.data_source is not None:
        sources.append(op.data_source)
    for child in op.inputs:
        _collect(child, counts, sources)


def _depth(op: PlanOperator) -> int:
    """Return the number of operators on the longest root-to-leaf path."""
    if not op.inputs:
        return 1
    return 1 + max(_depth(child) for child in op.inputs)


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _string_list(value: Any) -> list[str]:
    """Coerce a JSON value into a list of strings, dropping non-string entries."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
