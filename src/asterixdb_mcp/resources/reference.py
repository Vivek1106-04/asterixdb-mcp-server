"""Static reference resources shipped inside the gateway (no runtime fetch).

Six curated, version-pinned references an LLM can read to ground itself in
AsterixDB SQL++ without guessing: syntax, built-in functions, index types, the
type system, error codes, and worked query examples. Hand-edited and versioned
with the gateway; they never call the cluster.
"""

from __future__ import annotations

from typing import Any

from ..builtins_catalog import all_builtins

REFERENCE_VERSION = "1.0"


def _wrap(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"reference": kind, "version": REFERENCE_VERSION, **payload}


def read_sqlpp_syntax() -> dict[str, Any]:
    """SQL++ syntax cheat-sheet."""
    return _wrap(
        "sqlpp-syntax",
        {
            "rules": [
                "Qualify datasets as Dataverse.Dataset (do not backtick the whole dotted name).",
                "Backtick only reserved-word identifiers, e.g. `Dataset`.",
                "SELECT VALUE expr returns bare values; SELECT a, b returns objects.",
                "Always include a LIMIT on exploratory SELECTs.",
                "Nested fields use dot-notation: d.address.city.",
                "Flatten arrays with UNNEST: FROM ds AS d UNNEST d.arr AS item.",
                "GROUP BY groups rows; filter groups with HAVING, rows with WHERE.",
                "Use named parameters ($name) instead of splicing literals.",
                "Pass tuning knobs via the compilerParameters argument, never a SET prefix.",
            ],
            "clauseOrder": "SELECT ... FROM ... [UNNEST] ... WHERE ... GROUP BY ... "
            "HAVING ... ORDER BY ... LIMIT ... OFFSET ...",
        },
    )


def read_builtin_functions() -> dict[str, Any]:
    """The curated built-in function catalog (same data as list_functions)."""
    return _wrap(
        "builtin-functions",
        {
            "note": "Curated subset of AsterixDB built-ins. Use list_functions for UDFs.",
            "functions": [
                {"name": fn.name, "category": fn.category, "summary": fn.summary}
                for fn in all_builtins()
            ],
        },
    )


def read_index_types() -> dict[str, Any]:
    """Secondary index types and when to use each."""
    return _wrap(
        "index-types",
        {
            "indexes": [
                {"type": "BTREE", "use": "Equality and range predicates on scalar fields."},
                {"type": "RTREE", "use": "Spatial predicates on geometry/point fields."},
                {
                    "type": "KEYWORD / NGRAM (inverted)",
                    "use": "Full-text / fuzzy string search (contains, similarity).",
                },
                {
                    "type": "PRIMARY",
                    "use": "Implicit on the primary key; point lookups by key.",
                },
            ],
            "hint": "Use check_index_usage to see whether a query uses an available index.",
        },
    )


def read_type_system() -> dict[str, Any]:
    """The AsterixDB data model and primitive types."""
    return _wrap(
        "type-system",
        {
            "primitives": [
                "boolean", "tinyint", "smallint", "integer", "bigint", "float", "double",
                "string", "date", "time", "datetime", "duration", "interval", "uuid", "binary",
            ],
            "collections": ["array ([...])", "multiset ({{...}})", "object ({...})"],
            "openVsClosed": (
                "CLOSED types fix the fields; OPEN types allow extra undeclared fields. "
                "Sample the data to discover undeclared fields on OPEN datasets."
            ),
            "unknowns": "MISSING (absent field) and NULL differ; MISSING = MISSING is never true.",
        },
    )


def read_error_codes() -> dict[str, Any]:
    """Common AsterixDB ASX error codes and their meaning."""
    return _wrap(
        "error-codes",
        {
            "codes": [
                {"code": "ASX1001/ASX1002", "meaning": "Syntax / parse error."},
                {"code": "ASX1063", "meaning": "A readonly query cannot contain a DML statement."},
                {"code": "ASX1073", "meaning": "Type mismatch in an expression."},
                {"code": "ASX1074", "meaning": "Ambiguous alias reference; qualify columns."},
                {"code": "ASX1077", "meaning": "Cannot find dataset/dataverse by that name."},
                {"code": "ASX1081", "meaning": "Cannot find a function with that signature."},
            ],
            "hint": "Use the explain_error prompt for a cause-and-fix walkthrough.",
        },
    )


def read_query_examples() -> dict[str, Any]:
    """Worked, copy-adaptable SQL++ examples."""
    return _wrap(
        "query-examples",
        {
            "examples": [
                {
                    "title": "Projected filter",
                    "sql": "SELECT b.name, b.city FROM Shop.Business AS b "
                    "WHERE b.state = 'NV' LIMIT 20;",
                },
                {
                    "title": "Grouped aggregation",
                    "sql": "SELECT b.city AS city, COUNT(*) AS n FROM Shop.Business AS b "
                    "GROUP BY b.city ORDER BY n DESC LIMIT 20;",
                },
                {
                    "title": "Join two datasets",
                    "sql": "SELECT b.name, r.stars FROM Shop.Business AS b "
                    "JOIN Shop.Review AS r ON b.business_id = r.business_id LIMIT 20;",
                },
                {
                    "title": "Unnest a nested array",
                    "sql": "SELECT d.id, tag FROM Shop.Business AS d "
                    "UNNEST d.tags AS tag LIMIT 20;",
                },
            ]
        },
    )
