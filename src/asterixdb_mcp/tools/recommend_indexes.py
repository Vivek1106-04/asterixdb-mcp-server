"""recommend_indexes: workload-driven secondary-index recommendations.

Given a workload of representative SQL++ SELECTs, compile each one (compile-only,
read-only) to its optimized logical plan, observe which datasets are full-scanned
and which filter fields have no covering secondary index, and return ranked
``CREATE INDEX`` recommendations.

AsterixDB has no hypothetical-index facility (no ``hypopg`` equivalent), so a
recommendation cannot be validated by simulating the index and re-costing the
plan. Instead it is scored off plan predicates and workload frequency: a field
that is filtered on, has no covering index, and forces a full scan across several
workload queries is the strongest candidate. The gateway is read-only — every
recommendation is emitted as a DDL string for a human or agent to run, never
executed here.

Field attribution is deliberately conservative. The optimizer rewrites
declared-field access to by-INDEX (the field name is lost), so field names are
read from the UNOPTIMIZED logical plan, where access is still by name; the
optimized plan is used for the full-scan and existing-index signals. A candidate
field is taken only from a ``field-access-by-name`` expression (so a value
literal like ``"Austin"`` is never mistaken for a field), and only when the query
touches a single dataset, because a multi-dataset (join) plan cannot reliably
attribute a renamed plan variable back to one dataset. Queries that filter but
yield no attributable field still surface as lower-confidence "review" entries
carrying the raw predicates, so the agent can pick the field from the WHERE
clause itself.

Defense-in-Depth:
- Layer 1: the schema states this is workload-only, compile-only, read-only, and
  that the returned DDL is advice the gateway will not run.
- Layer 2: each statement is guarded and compiled in isolation; one that does not
  compile is recorded as ``skipped`` with its classified error rather than
  failing the whole batch, and only field-access expressions yield candidate
  fields so value literals can never become spurious index recommendations.
"""

from __future__ import annotations

import re
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError, classify_cc_error
from ..index_catalog import SecondaryIndex, fetch_secondary_indexes
from ..plan_parser import (
    UNOPTIMIZED_PLAN_KEY,
    ParsedPlan,
    datasets_from_sources,
    parse_optimized_plan,
    parse_plan,
)
from ..statement_guard import check_unsupported_functions, strip_set_prefix
from . import ToolResult

# Bound the batch so a caller cannot fan out an unbounded number of compile
# round-trips, and bound the output so the ranking stays focused on the top
# candidates rather than every field ever filtered.
MAX_STATEMENTS = 25
MAX_RECOMMENDATIONS = 20

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

# A full scan under a predicate is the signal that a field-level index would help;
# weight those observations above non-scanning ones when ranking.
_FULL_SCAN_WEIGHT = 2
_INDEXED_SCAN_WEIGHT = 1

# Matches AsterixDB's `field-access-by-name($$var, <name>)` accessor, where the
# name renders either as `AString: {fieldName}` (the expression printer's form,
# seen in the unoptimized plan) or as a quoted `"fieldName"`. The name argument of
# field-access-by-name is always a field name — a filter VALUE literal appears
# elsewhere in the expression, never here — so this never mistakes a value for a
# field. The optimizer rewrites this to field-access-by-INDEX (name lost), which
# is why field names are read from the unoptimized plan.
_FIELD_ACCESS_RE = re.compile(
    r"field-access-by-name\([^,()]+,\s*(?:AString:\s*\{([^}]+)\}|\"([^\"]+)\")\)"
)


async def run_recommend_indexes(
    client: CCClient,
    settings: Settings,
    *,
    statements: list[str],
    dataverse: str | None = None,
    user_tag: str | None = None,
) -> ToolResult:
    """Recommend secondary indexes from a workload of SQL++ SELECTs."""
    if not statements:
        return ToolResult.error(
            GatewayError(
                ErrorType.INVALID_PARAMETER,
                "Provide at least one SQL++ SELECT statement in `statements`.",
            )
        )
    if len(statements) > MAX_STATEMENTS:
        return ToolResult.error(
            GatewayError(
                ErrorType.INVALID_PARAMETER,
                f"Too many statements ({len(statements)}); pass at most {MAX_STATEMENTS}.",
            )
        )

    ccid = make_client_context_id(settings.agent_session_id, user_tag)
    indexes = await fetch_secondary_indexes(client, ccid, dataverse=dataverse)
    covered = _covered_first_keys(indexes)

    observations: list[_Observation] = []
    analyzed = 0
    skipped: list[dict[str, Any]] = []
    for statement in statements:
        outcome = await _analyze_statement(
            client, settings, statement, dataverse, user_tag
        )
        if isinstance(outcome, dict):
            skipped.append(outcome)
            continue
        analyzed += 1
        observations.extend(outcome)

    recommendations = _rank(_aggregate(observations, covered))
    structured: dict[str, Any] = {
        "status": "success",
        "dataverseFilter": dataverse,
        "statementsSubmitted": len(statements),
        "statementsAnalyzed": analyzed,
        "indexesKnown": len(indexes),
        "recommendationCount": len(recommendations),
        "recommendations": recommendations,
        "skipped": skipped,
    }
    return ToolResult(text=_summarize(structured), structured=structured)


# per-statement analysis (I/O)


class _Observation:
    """One (dataset, did-it-full-scan, candidate fields) record from a plan."""

    __slots__ = ("dataset", "dataverse", "fields", "full_scan", "predicates")

    def __init__(
        self,
        dataverse: str,
        dataset: str,
        *,
        full_scan: bool,
        fields: tuple[str, ...],
        predicates: tuple[str, ...],
    ) -> None:
        self.dataverse = dataverse
        self.dataset = dataset
        self.full_scan = full_scan
        self.fields = fields
        self.predicates = predicates


async def _analyze_statement(
    client: CCClient,
    settings: Settings,
    statement: str,
    dataverse: str | None,
    user_tag: str | None,
) -> list[_Observation] | dict[str, Any]:
    """Compile one statement to a plan and derive observations, or a skip record.

    Returns a list of observations on success, or a ``skipped`` dict describing
    why this statement contributed nothing — a guard rejection or a compile error
    — so one bad query never aborts the batch.
    """
    cleaned = strip_set_prefix(statement)
    if not cleaned.strip():
        return _skip(statement, ErrorType.INVALID_PARAMETER, "Empty statement.")
    bad_function = check_unsupported_functions(cleaned)
    if bad_function is not None:
        return _skip(statement, ErrorType.INVALID_PARAMETER, bad_function.message)

    ccid = make_client_context_id(settings.agent_session_id, user_tag)
    try:
        envelope = await client.compile_query(
            cleaned,
            client_context_id=ccid,
            dataverse=dataverse,
            emit_plan=True,
            emit_unoptimized_plan=True,
        )
    except GatewayError as err:
        return _skip(statement, err.error_type, err.message)

    error = _classify_envelope_error(envelope)
    if error is not None:
        return _skip(statement, error.error_type, error.message)

    plans = envelope.get("plans")
    optimized = parse_optimized_plan(plans)
    if optimized is None:
        return _skip(statement, ErrorType.INTERNAL, "No optimized plan returned.")
    unoptimized = parse_plan(plans, UNOPTIMIZED_PLAN_KEY)
    return _observe(optimized, unoptimized, dataverse)


def _observe(
    optimized: ParsedPlan, unoptimized: ParsedPlan | None, dataverse: str | None
) -> list[_Observation]:
    """Derive per-dataset observations from one query's plans.

    Datasets scanned and the full-scan flag come from the OPTIMIZED plan (the
    optimizer is the authority on access paths). Filter field names come from the
    UNOPTIMIZED plan, which still accesses fields by name; the optimized plan has
    rewritten declared fields to by-index, losing the name. Fields are attributed
    only when the query touches exactly one dataset — across a join a renamed plan
    variable cannot be safely tied to one dataset, so fields are dropped and the
    predicates ride along for human review instead.
    """
    datasets = datasets_from_sources(optimized.data_sources, dataverse)
    if not datasets:
        return []
    predicate_source = unoptimized if unoptimized is not None else optimized
    predicates = _all_predicates(predicate_source)
    single_dataset = len(datasets) == 1
    fields = extract_fields_from_predicates(predicates) if single_dataset else ()
    full_scan = (
        "data-scan" in optimized.operator_counts
        and "unnest-map" not in optimized.operator_counts
    )
    return [
        _Observation(dv, ds, full_scan=full_scan, fields=fields, predicates=predicates)
        for dv, ds in datasets
    ]


# pure analysis (unit-tested without I/O)


def extract_fields_from_predicates(predicates: tuple[str, ...]) -> tuple[str, ...]:
    """Pull candidate field paths from ``field-access-by-name`` expressions.

    Order-preserving and de-duplicated. Returns an empty tuple when no field
    accessor is present (e.g. the predicate filters only on plan variables).
    """
    seen: set[str] = set()
    out: list[str] = []
    for predicate in predicates:
        for astring_name, quoted_name in _FIELD_ACCESS_RE.findall(predicate):
            field = astring_name or quoted_name
            if field and field not in seen:
                seen.add(field)
                out.append(field)
    return tuple(out)


def _aggregate(
    observations: list[_Observation], covered: dict[tuple[str, str], set[str]]
) -> dict[tuple[str, str, str | None], dict[str, Any]]:
    """Fold observations into candidate (dataverse, dataset, field) records.

    A field already served by an existing secondary index (it is the first key of
    some index on that dataset) is skipped — there is nothing to recommend. A
    dataset filtered with no attributable field becomes a single field=None
    "review" candidate carrying the raw predicates.
    """
    candidates: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for obs in observations:
        location = (obs.dataverse, obs.dataset)
        weight = _FULL_SCAN_WEIGHT if obs.full_scan else _INDEXED_SCAN_WEIGHT
        novel_fields = [f for f in obs.fields if f not in covered.get(location, set())]
        if novel_fields:
            for field in novel_fields:
                _accumulate(candidates, obs, field, weight)
        elif obs.full_scan and not obs.fields:
            # Filtered (or scanned) but no field we can pin: surface for review.
            _accumulate(candidates, obs, None, weight)
    return candidates


def _accumulate(
    candidates: dict[tuple[str, str, str | None], dict[str, Any]],
    obs: _Observation,
    field: str | None,
    weight: int,
) -> None:
    """Add one observation's weight to a candidate, creating it if new."""
    key = (obs.dataverse, obs.dataset, field)
    record = candidates.get(key)
    if record is None:
        record = {
            "dataverse": obs.dataverse,
            "dataset": obs.dataset,
            "field": field,
            "score": 0,
            "supportingStatements": 0,
            "usesFullScan": False,
            "predicates": [],
        }
        candidates[key] = record
    record["score"] += weight
    record["supportingStatements"] += 1
    record["usesFullScan"] = record["usesFullScan"] or obs.full_scan
    for predicate in obs.predicates:
        if predicate not in record["predicates"]:
            record["predicates"].append(predicate)


def _rank(
    candidates: dict[tuple[str, str, str | None], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Finalize candidates into ranked recommendations with DDL and confidence."""
    recommendations = [_finalize(record) for record in candidates.values()]
    recommendations.sort(
        key=lambda r: (-r["score"], r["dataverse"], r["dataset"], r["field"] or "~")
    )
    return recommendations[:MAX_RECOMMENDATIONS]


def _finalize(record: dict[str, Any]) -> dict[str, Any]:
    """Attach a CREATE INDEX recommendation, confidence, and rationale."""
    dv, ds, field = record["dataverse"], record["dataset"], record["field"]
    if field is not None:
        confidence = CONFIDENCE_HIGH if record["usesFullScan"] else CONFIDENCE_MEDIUM
        index_name = _index_name(ds, field)
        record["recommendedDDL"] = f"CREATE INDEX {index_name} ON {dv}.{ds}({field});"
        record["rationale"] = (
            f"{field} is filtered by {record['supportingStatements']} workload "
            f"statement(s) on {dv}.{ds} with no covering secondary index"
            + (
                "; those queries fall back to a full scan."
                if record["usesFullScan"]
                else "."
            )
        )
    else:
        confidence = CONFIDENCE_LOW
        record["recommendedDDL"] = None
        record["rationale"] = (
            f"{dv}.{ds} is full-scanned by {record['supportingStatements']} workload "
            "statement(s) but no filter field could be attributed from the plan. "
            "Inspect the WHERE clause and index the most selective equality/range field."
        )
    record["confidence"] = confidence
    return record


# helpers


def _covered_first_keys(indexes: list[SecondaryIndex]) -> dict[tuple[str, str], set[str]]:
    """Map (dataverse, dataset) to the set of fields that lead an existing index.

    Only the first key field is treated as "covered": a composite index on
    ``(a, b)`` already serves an equality/range filter on ``a``, so recommending a
    standalone index on ``a`` would be redundant, while ``b`` alone is not served.
    """
    covered: dict[tuple[str, str], set[str]] = {}
    for index in indexes:
        if index.dataverse is None or index.dataset is None or not index.key_fields:
            continue
        covered.setdefault((index.dataverse, index.dataset), set()).add(index.key_fields[0])
    return covered


def _all_predicates(parsed: ParsedPlan) -> tuple[str, ...]:
    """Collect every predicate string across the plan tree, de-duplicated."""
    seen: set[str] = set()
    out: list[str] = []
    stack = [parsed.root]
    while stack:
        node = stack.pop()
        for predicate in node.predicates:
            if predicate not in seen:
                seen.add(predicate)
                out.append(predicate)
        stack.extend(node.inputs)
    return tuple(out)


def _index_name(dataset: str, field: str) -> str:
    """Build a readable, identifier-safe index name from a dataset and field."""
    safe_field = re.sub(r"[^0-9A-Za-z]+", "_", field).strip("_") or "field"
    return f"idx_{dataset}_{safe_field}"


def _skip(statement: str, error_type: ErrorType, message: str) -> dict[str, Any]:
    """Build a skip record (statement preview + why it contributed nothing)."""
    preview = statement.strip()
    if len(preview) > 120:
        preview = preview[:117] + "..."
    return {"statement": preview, "errorType": error_type.value, "reason": message}


def _classify_envelope_error(envelope: dict[str, Any]) -> GatewayError | None:
    errors = envelope.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        code = errors[0].get("code")
        message = errors[0].get("msg")
        return classify_cc_error(
            asterix_code=code if isinstance(code, str) else None,
            message=message if isinstance(message, str) else "AsterixDB returned an error.",
        )
    return None


def _summarize(structured: dict[str, Any]) -> str:
    """One-line human summary for the content text block."""
    n = structured["recommendationCount"]
    analyzed = structured["statementsAnalyzed"]
    submitted = structured["statementsSubmitted"]
    if n == 0:
        return (
            f"No index recommendations from {analyzed}/{submitted} analyzed statement(s): "
            "every filtered field is already indexed, or no plan exposed a filter field."
        )
    return (
        f"{n} index recommendation(s) from {analyzed}/{submitted} analyzed statement(s), "
        "ranked by workload frequency and full-scan impact. Each recommendedDDL is advice — "
        "the gateway will not run it."
    )
