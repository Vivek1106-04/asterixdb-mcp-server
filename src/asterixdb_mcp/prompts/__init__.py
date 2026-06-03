"""Prompt core logic.

Prompts are parameterized templates that inject schema, rules, or workflow
scaffolding into the LLM's context. They never auto-execute; they produce text
the agent reasons about, then invokes tools separately (the two-step safety
contract). The pure compose_* builders are kept apart from I/O so the template
unit-tests directly.
"""

from __future__ import annotations

# Injected into every prompt scoped to a specific Dataset.
STORAGE_FORMAT_AWARENESS_BLOCK = """\
## Storage-Format Awareness

Each Dataset reports a `datasetFormatInfo.format` field.

If format == "COLUMNAR":
1. NEVER write SELECT *. Always project explicit fields.
2. Prefer queries that touch the fewest column groups. Fields in the same group
   are cheap together; spanning groups is expensive.
3. Aggregations on a single numeric column are ~10-100x cheaper than ROW.
   Lean into wide aggregations.
4. Predicate pushdown matters more: `WHERE eventType = 'click'` evaluated in
   the column group avoids fetching unrelated columns.

If format == "ROW":
1. Projection still helps but less dramatically (~1.5-3x).
2. Index usage dominates cost; prefer indexed predicates.
3. Wide row reads (SELECT *) are acceptable when you genuinely need all fields.

Default to columnar-aware behavior unless the format is confirmed ROW."""
