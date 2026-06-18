"""recommend_indexes: secondary-index recommendations for a SQL++ workload.

Primary path is AsterixDB's **native** cost-based index advisor: the engine
accepts ``ADVISE <query>``, drives the optimizer with hypothetical ("fake")
indexes, and returns the indexes it would create. The gateway templates
``ADVISE`` in front of each workload statement, forwards it read-only (the
advisor analyzes, it does not execute the query), and aggregates the engine's
``recommended_indexes`` across the workload. This is CBO-costed, join-aware, and
is the cluster's own recommendation — strictly better than guessing from plan
shape.

A heuristic fallback covers clusters/builds where ``ADVISE`` is unavailable: it
compiles the statement (compile-only, read-only), reads filter field names from
the unoptimized logical plan (the optimizer rewrites declared-field access to
by-index, losing the name), and scores fields that are filtered, uncovered by an
existing index, and forcing a full scan. Fields are attributed only for
single-dataset plans; a join yields a lower-confidence review entry.

The gateway is read-only either way: every recommendation is a ``CREATE INDEX``
string for a human or agent to run, never executed here.

Defense-in-Depth:
- Layer 1: the schema states this is workload-only, read-only, and that the
  returned DDL is advice the gateway will not run.
- Layer 2: each statement is guarded and analyzed in isolation; one that the
  advisor rejects falls back to the heuristic, and one that neither path can use
  is recorded in ``skipped`` rather than failing the batch. Only field-access
  expressions yield heuristic candidate fields, so value literals never become
  spurious recommendations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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

# Bound the batch so a caller cannot fan out an unbounded number of round-trips,
# and bound the output so the ranking stays focused on the top candidates.
MAX_STATEMENTS = 25
MAX_RECOMMENDATIONS = 20

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

SOURCE_ADVISE = "advise"
SOURCE_HEURISTIC = "heuristic"

# A full scan under a predicate is the signal that a field-level index would help;
# weight those observations above non-scanning ones when ranking (heuristic path).
_FULL_SCAN_WEIGHT = 2
_INDEXED_SCAN_WEIGHT = 1

# Matches AsterixDB's `field-access-by-name($$var, <name>)` accessor, where the
# name renders either as `AString: {fieldName}` (the expression printer's form,
# seen in the unoptimized plan) or as a quoted `"fieldName"`. The name argument of
# field-access-by-name is always a field name — a filter VALUE literal appears
# elsewhere — so this never mistakes a value for a field.
_FIELD_ACCESS_RE = re.compile(
    r"field-access-by-name\([^,()]+,\s*(?:AString:\s*\{([^}]+)\}|\"([^\"]+)\")\)"
)

# Parses the engine advisor's `CREATE INDEX <name> ON `db`.`dv`.`ds`(f1, f2)`
# form (AdviseIndexRule.getCreateIndexClause) to recover dataverse/dataset/fields
# for grouping and display. This parses the gateway-received engine string, never
# user SQL++ — the architecture invariant (Gateway never parses SQL++) is intact.
_ADVISE_DDL_RE = re.compile(r"ON\s+`[^`]+`\.`([^`]+)`\.`([^`]+)`\s*\(([^)]*)\)")


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

    native: dict[str, dict[str, Any]] = {}
    current_indexes: set[str] = set()
    observations: list[_Observation] = []
    analyzed = 0
    native_used = 0
    skipped: list[dict[str, Any]] = []

    for statement in statements:
        guard = _guard(statement)
        if guard is not None:
            skipped.append(guard)
            continue
        advice = await _advise_statement(client, settings, statement, dataverse, user_tag)
        if advice is not None:
            analyzed += 1
            native_used += 1
            current_indexes.update(advice.current)
            for ddl in advice.recommended:
                _accumulate_native(native, ddl)
            continue
        # Advisor unavailable for this statement: fall back to the heuristic plan path.
        outcome = await _analyze_statement(client, settings, statement, dataverse, user_tag)
        if isinstance(outcome, dict):
            skipped.append(outcome)
            continue
        analyzed += 1
        observations.extend(outcome)

    recommendations = _merge(native, _rank(_aggregate(observations, covered)))
    method = SOURCE_ADVISE if native_used == analyzed and analyzed else (
        SOURCE_HEURISTIC if native_used == 0 else "mixed"
    )
    structured: dict[str, Any] = {
        "status": "success",
        "method": method,
        "dataverseFilter": dataverse,
        "statementsSubmitted": len(statements),
        "statementsAnalyzed": analyzed,
        "nativeAdviseStatements": native_used,
        "indexesKnown": len(indexes),
        "currentIndexes": sorted(current_indexes),
        "recommendationCount": len(recommendations),
        "recommendations": recommendations,
        "skipped": skipped,
    }
    return ToolResult(text=_summarize(structured), structured=structured)


# native ADVISE path (I/O)


@dataclass(frozen=True)
class _Advice:
    """The advisor's verdict for one statement: which indexes to create / exist."""

    recommended: tuple[str, ...]
    current: tuple[str, ...]


async def _advise_statement(
    client: CCClient,
    settings: Settings,
    statement: str,
    dataverse: str | None,
    user_tag: str | None,
) -> _Advice | None:
    """Run ``ADVISE <statement>`` and parse the advisor result.

    Returns the parsed advice on success, or None when the advisor is unavailable
    or returns nothing usable (transport error, the cluster does not support
    ADVISE, or the statement is not a query) — the caller then falls back to the
    heuristic path. The statement is already guarded by the caller.
    """
    cleaned = strip_set_prefix(statement)
    ccid = make_client_context_id(settings.agent_session_id, user_tag)
    try:
        envelope = await client.execute(
            f"ADVISE {cleaned}", client_context_id=ccid, dataverse=dataverse
        )
    except GatewayError:
        # execute() raises on an error envelope (e.g. a cluster that does not
        # support ADVISE rejects the keyword), so the heuristic fallback runs.
        return None
    return _parse_advise(envelope.get("results"))


def _parse_advise(results: Any) -> _Advice | None:
    """Extract recommended/current ``CREATE INDEX`` strings from an ADVISE result.

    The advisor returns one ``Advise`` object whose ``advice.adviseinfo`` holds
    ``recommended_indexes.indexes`` and ``current_indexes``, each entry an
    ``{"index_statement": "CREATE INDEX ..."}`` record. Returns None when no such
    object is present, signalling the fallback path.
    """
    if not isinstance(results, list):
        return None
    for row in results:
        if not isinstance(row, dict):
            continue
        adviseinfo = row.get("advice", {}).get("adviseinfo") if isinstance(
            row.get("advice"), dict
        ) else None
        if not isinstance(adviseinfo, dict):
            continue
        recommended = _index_statements(
            adviseinfo.get("recommended_indexes", {}).get("indexes")
            if isinstance(adviseinfo.get("recommended_indexes"), dict)
            else None
        )
        current = _index_statements(adviseinfo.get("current_indexes"))
        return _Advice(recommended=recommended, current=current)
    return None


def _index_statements(entries: Any) -> tuple[str, ...]:
    """Pull the ``index_statement`` strings out of an advisor index list."""
    if not isinstance(entries, list):
        return ()
    out: list[str] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("index_statement"), str):
            out.append(entry["index_statement"].strip())
    return tuple(out)


def _accumulate_native(native: dict[str, dict[str, Any]], ddl: str) -> None:
    """Add one advisor DDL to the native recommendation set, counting support."""
    record = native.get(ddl)
    if record is None:
        dv, ds, field = _parse_advise_ddl(ddl)
        record = {
            "source": SOURCE_ADVISE,
            "confidence": CONFIDENCE_HIGH,
            "dataverse": dv,
            "dataset": ds,
            "field": field,
            "recommendedDDL": ddl if ddl.endswith(";") else f"{ddl};",
            "supportingStatements": 0,
        }
        native[ddl] = record
    record["supportingStatements"] += 1


def _native_recommendations(native: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Finalize and rank the native advisor recommendations."""
    out = list(native.values())
    for record in out:
        n = record["supportingStatements"]
        record["rationale"] = (
            f"AsterixDB's cost-based advisor (ADVISE) recommends this index for "
            f"{n} workload statement(s)."
        )
    out.sort(key=lambda r: (-r["supportingStatements"], r["recommendedDDL"]))
    return out


def _merge(
    native: dict[str, dict[str, Any]], heuristic: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Native advisor recommendations first, then heuristic, capped."""
    return (_native_recommendations(native) + heuristic)[:MAX_RECOMMENDATIONS]


# heuristic fallback path (I/O)


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
    """Compile one statement to its plans and derive observations, or a skip record."""
    cleaned = strip_set_prefix(statement)
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

    Datasets scanned and the full-scan flag come from the OPTIMIZED plan; filter
    field names come from the UNOPTIMIZED plan, which still accesses fields by
    name. Fields are attributed only when the query touches exactly one dataset —
    across a join a renamed plan variable cannot be tied to one dataset.
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
    """Pull candidate field paths from ``field-access-by-name`` expressions."""
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
    """Fold observations into candidate (dataverse, dataset, field) records."""
    candidates: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for obs in observations:
        location = (obs.dataverse, obs.dataset)
        weight = _FULL_SCAN_WEIGHT if obs.full_scan else _INDEXED_SCAN_WEIGHT
        novel_fields = [f for f in obs.fields if f not in covered.get(location, set())]
        if novel_fields:
            for field in novel_fields:
                _accumulate(candidates, obs, field, weight)
        elif obs.full_scan and not obs.fields:
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
            "source": SOURCE_HEURISTIC,
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
    """Finalize heuristic candidates into ranked recommendations."""
    recommendations = [_finalize(record) for record in candidates.values()]
    recommendations.sort(
        key=lambda r: (-r["score"], r["dataverse"], r["dataset"], r["field"] or "~")
    )
    return recommendations


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


def _guard(statement: str) -> dict[str, Any] | None:
    """Reject a statement both paths would reject, returning a skip record or None."""
    cleaned = strip_set_prefix(statement)
    if not cleaned.strip():
        return _skip(statement, ErrorType.INVALID_PARAMETER, "Empty statement.")
    bad_function = check_unsupported_functions(cleaned)
    if bad_function is not None:
        return _skip(statement, ErrorType.INVALID_PARAMETER, bad_function.message)
    return None


def _covered_first_keys(indexes: list[SecondaryIndex]) -> dict[tuple[str, str], set[str]]:
    """Map (dataverse, dataset) to the set of fields that lead an existing index."""
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


def _parse_advise_ddl(ddl: str) -> tuple[str | None, str | None, str | None]:
    """Best-effort (dataverse, dataset, leadingField) from an advisor DDL string."""
    match = _ADVISE_DDL_RE.search(ddl)
    if match is None:
        return (None, None, None)
    dataverse, dataset, fields = match.groups()
    first_field = fields.split(",")[0].strip().strip("`") or None
    return (dataverse, dataset, first_field)


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
    via = (
        "the native ADVISE advisor"
        if structured["method"] == SOURCE_ADVISE
        else "a heuristic plan scan"
        if structured["method"] == SOURCE_HEURISTIC
        else "the native advisor with a heuristic fallback"
    )
    if n == 0:
        return (
            f"No index recommendations from {analyzed}/{submitted} analyzed statement(s) "
            f"via {via}: existing indexes already cover the workload's filters."
        )
    return (
        f"{n} index recommendation(s) from {analyzed}/{submitted} analyzed statement(s) "
        f"via {via}. Each recommendedDDL is advice — the gateway will not run it."
    )
