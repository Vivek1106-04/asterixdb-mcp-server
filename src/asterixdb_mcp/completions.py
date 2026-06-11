"""Argument completion for prompts and resource templates.

High-end MCP clients call ``completion/complete`` to autocomplete an argument as
the user types it. Grounding ``dataverse``/``dataset``/field-name arguments in
live metadata turns blind guessing into selection from real names, which removes
a whole class of "cannot find dataset" (ASX1077) failures before a query is ever
written.

Security model (defense in depth):

- Layer 1 — the request is well-typed by the MCP SDK (``CompletionArgument``).
- Layer 2 — this module guards every resolution: the partial value is length-
  capped, unknown arguments yield no completion, candidate names are fetched
  through the existing read-only discovery tools (never by splicing the partial
  into SQL), results are deduplicated and hard-capped, and any cluster error is
  swallowed into an empty completion rather than surfaced. There is no injection
  surface because the partial value is only ever used as an in-process substring
  filter over names the cluster already returned.
"""

from __future__ import annotations

from typing import Any

from mcp.types import Completion

from .cc_client import CCClient
from .config import Settings
from .tools.get_schema import run_get_schema
from .tools.list_datasets import run_list_datasets
from .tools.list_dataverses import run_list_dataverses

# The MCP completion response is capped at 100 values by the specification.
MAX_COMPLETION_VALUES = 100
# Guard against a pathologically long partial value driving the filter.
MAX_PARTIAL_LEN = 256

# Argument names this server knows how to complete from metadata.
_DATAVERSE_ARG = "dataverse"
_DATASET_ARG = "dataset"
_FIELD_ARGS = frozenset({"group_by", "metric"})


def rank_completions(candidates: list[str | None], partial: str | None) -> Completion:
    """Rank candidate names against a partial value (pure, no I/O).

    Ranking: exact match > prefix match > substring match, each case-insensitive,
    then alphabetical. An empty partial returns every candidate alphabetically.
    Duplicate names collapse to their best score. The value list is hard-capped
    at ``MAX_COMPLETION_VALUES`` and ``hasMore`` reports truncation.
    """
    needle = (partial or "").strip().lower()[:MAX_PARTIAL_LEN]
    best: dict[str, int] = {}
    for raw in candidates:
        if not raw:
            continue
        name = str(raw)
        score = _match_score(name.lower(), needle)
        if score is None:
            continue
        if name not in best or score > best[name]:
            best[name] = score

    ordered = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
    names = [name for name, _ in ordered]
    values = names[:MAX_COMPLETION_VALUES]
    return Completion(values=values, total=len(names), hasMore=len(names) > len(values))


def _match_score(name_lower: str, needle: str) -> int | None:
    """Score a single candidate; None means it does not match the partial."""
    if not needle:
        return 0
    if name_lower == needle:
        return 3
    if name_lower.startswith(needle):
        return 2
    if needle in name_lower:
        return 1
    return None


def _resolved_arg(context_arguments: dict[str, str] | None, name: str) -> str | None:
    """Read a previously-resolved argument value from the completion context."""
    if not context_arguments:
        return None
    value = context_arguments.get(name)
    return value or None


async def complete_argument(
    client: CCClient,
    settings: Settings,
    *,
    argument_name: str,
    partial: str | None,
    context_arguments: dict[str, str] | None = None,
) -> Completion | None:
    """Resolve a completion for one argument, or None if it is not completable.

    Cluster errors are swallowed into an empty completion (never raised), so a
    transient cluster issue degrades autocomplete instead of breaking the client.
    """
    if argument_name == _DATAVERSE_ARG:
        return await _complete_dataverse(client, settings, partial)
    if argument_name == _DATASET_ARG:
        dataverse = _resolved_arg(context_arguments, _DATAVERSE_ARG)
        return await _complete_dataset(client, settings, partial, dataverse)
    if argument_name in _FIELD_ARGS:
        dataverse = _resolved_arg(context_arguments, _DATAVERSE_ARG)
        dataset = _resolved_arg(context_arguments, _DATASET_ARG)
        return await _complete_field(client, settings, partial, dataverse, dataset)
    return None


async def _complete_dataverse(
    client: CCClient, settings: Settings, partial: str | None
) -> Completion:
    result = await run_list_dataverses(client, settings)
    if result.is_error:
        return _empty()
    rows = result.structured.get("dataverses", [])
    return rank_completions([r.get("dataverse") for r in rows], partial)


async def _complete_dataset(
    client: CCClient, settings: Settings, partial: str | None, dataverse: str | None
) -> Completion:
    result = await run_list_datasets(client, settings, dataverse=dataverse)
    if result.is_error:
        return _empty()
    rows = result.structured.get("datasets", [])
    return rank_completions([r.get("dataset") for r in rows], partial)


async def _complete_field(
    client: CCClient,
    settings: Settings,
    partial: str | None,
    dataverse: str | None,
    dataset: str | None,
) -> Completion:
    # Field names need a concrete dataset; without one there is nothing to ground on.
    if not dataverse or not dataset:
        return _empty()
    result = await run_get_schema(client, settings, dataverse=dataverse, dataset=dataset)
    if result.is_error:
        return _empty()
    fields: list[dict[str, Any]] = result.structured.get("fields", [])
    return rank_completions([f.get("name") for f in fields], partial)


def _empty() -> Completion:
    return Completion(values=[], total=0, hasMore=False)
