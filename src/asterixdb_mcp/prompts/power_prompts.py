"""Workflow power prompts for common analytical tasks.

Each is a pure text builder that scaffolds a workflow into the agent's context;
none auto-executes (the two-step safety contract). Dataset-scoped prompts embed
the storage-format awareness block so every generated query stays columnar-aware.
"""

from __future__ import annotations

from . import STORAGE_FORMAT_AWARENESS_BLOCK

# Plain templates with placeholder tokens (no string interpolation), rendered by
# token replacement so a SQL example in the body is never built by f-string,
# %-format, or concatenation.
_BUILD_AGGREGATION_TEMPLATE = """\
# Build an aggregation over __DATAVERSE__.__DATASET__

Goal: produce a correct, columnar-aware GROUP BY query.

Steps:
1. Call get_schema for __DATAVERSE__.__DATASET__ and copy exact field names
   (casing matters). Note `datasetFormatInfo.format`.
2. Project only the grouping key(s) and the aggregate — never select every column.
3. Use AsterixDB aggregate names: COUNT, SUM, AVG, MIN, MAX, STDDEV_SAMP,
   STDDEV_POP, VAR_SAMP, VAR_POP, ARRAY_AGG. Do NOT nest one aggregate inside
   another in the same projection.
4. Filter post-aggregation with HAVING, not WHERE.
5. Validate with validate_syntax before running; then execute_query with a LIMIT.

Template (fill the placeholders):
```sql
SELECT g.__GROUP__ AS grp, __METRIC__ AS value
FROM __DATAVERSE__.__DATASET__ AS g
WHERE <row-level predicate>          -- optional, prunes rows before grouping
GROUP BY g.__GROUP__
HAVING __METRIC__ > <threshold>      -- optional, filters groups
ORDER BY value DESC
LIMIT 20;
```"""


def compose_build_aggregation_query(
    dataverse: str | None = None,
    dataset: str | None = None,
    group_by: str | None = None,
    metric: str | None = None,
) -> str:
    """Scaffold a GROUP BY + HAVING aggregation against one dataset."""
    group_hint = group_by or "<the grouping field>"
    metric_hint = metric or "<the metric, e.g. COUNT(*), AVG(field)>"
    body = (
        _BUILD_AGGREGATION_TEMPLATE.replace("__DATAVERSE__", dataverse or "<dataverse>")
        .replace("__DATASET__", dataset or "<dataset>")
        .replace("__GROUP__", group_hint)
        .replace("__METRIC__", metric_hint)
    )
    return "\n\n".join((body, STORAGE_FORMAT_AWARENESS_BLOCK))


def compose_analyze_query_performance(statement: str | None = None) -> str:
    """Guide profiling a query and interpreting the metrics block."""
    target = f"\n\nQuery under analysis:\n```sql\n{statement}\n```" if statement else ""
    return f"""\
# Analyze query performance

Steps:
1. Run the query with execute_query and `profile=true` (or submit_async_query for
   a long one). Read the returned `metrics` block.
2. Interpret:
   - `processedObjects` high but `resultCount` low -> a full scan with a late
     filter; tighten the WHERE or add an index (see recommend_indexes).
   - `elapsedTime` >> `executionTime` -> queueing or result transport, not compute.
   - large `resultSize` -> projecting too many columns; project fewer.
3. Cross-check access paths with explain_query and check_index_usage.
4. Re-run after each change and compare metrics; change one thing at a time.{target}"""


def compose_recommend_indexes(
    dataverse: str | None = None, dataset: str | None = None
) -> str:
    """Chain check_index_usage into a CREATE INDEX recommendation."""
    dataverse = dataverse or "<dataverse>"
    dataset = dataset or "<dataset>"
    return f"""\
# Recommend indexes for {dataverse}.{dataset}

Steps:
1. Take the slow query. Call check_index_usage with it.
2. If `usesFullScan` is true or `availableButUnused` lists no helpful index, the
   query has no selective index for its predicate.
3. Identify the most selective equality/range field(s) in the WHERE clause from
   get_schema.
4. Recommend (do NOT execute — this gateway is read-only) a DDL like:
   ```sql
   CREATE INDEX idx_{dataset}_<field> ON {dataverse}.{dataset}(<field>);
   ```
5. Explain the expected effect: an indexed predicate turns a full scan into a
   point/range lookup. Re-run check_index_usage mentally against the new index.

Only recommend an index a real predicate would use; do not suggest indexes
speculatively."""


_EXPLORE_NESTED_TEMPLATE = """\
# Explore nested data in __DATAVERSE__.__DATASET__

Steps:
1. Call sample_dataset for __DATAVERSE__.__DATASET__ to see real document shapes,
   including nested objects and arrays not in the declared schema.
2. Discover object fields dynamically with OBJECT_NAMES(doc).
3. Flatten arrays with UNNEST:
   ```sql
   SELECT d.id, item
   FROM __DATAVERSE__.__DATASET__ AS d
   UNNEST d.<arrayField> AS item
   LIMIT 20;
   ```
4. Reach into nested objects with dot-notation: `d.address.city`.
5. Always keep a LIMIT while exploring; nested UNNEST can multiply row counts."""


def compose_explore_nested_data(
    dataverse: str | None = None, dataset: str | None = None
) -> str:
    """Guide UNNEST / OBJECT_NAMES traversal of nested documents."""
    body = _EXPLORE_NESTED_TEMPLATE.replace(
        "__DATAVERSE__", dataverse or "<dataverse>"
    ).replace("__DATASET__", dataset or "<dataset>")
    return "\n\n".join((body, STORAGE_FORMAT_AWARENESS_BLOCK))


def compose_explain_error(error: str | None = None) -> str:
    """Translate an AsterixDB error code/message into cause + fix."""
    error = error or "<paste the AsterixDB error code or message here>"
    return f"""\
# Explain and fix this error

Reported error:
```
{error}
```

How to resolve:
1. Read the error class:
   - ASX1077 "Cannot find dataset" -> the name or its dataverse is wrong; call
     list_datasets / search_metadata and copy the exact name and casing.
   - ASX1073/"type mismatch" -> a field or literal has an unexpected type; sample
     the data and cast explicitly (to_number/to_string).
   - ASX1081 "Cannot find function" -> wrong function name; call list_functions.
     Standard deviation is STDDEV_SAMP/STDDEV_POP (there is no STDEV).
   - ASX1001/ASX1002 syntax/parse -> malformed SQL++; validate_syntax shows where.
   - ASX1074 "ambiguous alias" -> qualify columns with their alias and do not nest
     an aggregate inside another aggregate.
2. Apply the fix, then re-check cheaply with validate_syntax before re-running.
3. If the cause is unclear, call explain_query to see how the statement compiles."""
