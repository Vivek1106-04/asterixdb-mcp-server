"""search_metadata: fuzzy search across the whole metadata catalog.

One entry point to answer "is there a dataset/type/index/function/synonym/feed
whose name looks like X?" without the model guessing. Pulls candidate names from
each Metadata collection and ranks them against the query with a cheap fuzzy
score (exact > prefix > substring > character similarity).

Defense-in-Depth:
- Layer 1: the schema lists exactly which object kinds are searched and that the
  query is matched against object NAMES.
- Layer 2: an empty/over-long query is rejected pre-flight; a per-collection
  query failure degrades to fewer results instead of failing the whole search.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError
from . import ToolResult

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_QUERY_LEN = 200

_EXACT_SCORE = 100
_PREFIX_SCORE = 80
_SUBSTRING_SCORE = 60
_SIMILARITY_WEIGHT = 50  # ratio (0..1) scaled into the tail below substring matches


@dataclass(frozen=True)
class _Collection:
    """A Metadata collection to search: its kind label and name field."""

    kind: str
    table: str
    name_field: str


# Built by token replacement (not f-string/concat) so the static SQL template is
# never assembled from interpolated fragments. The table name comes from the
# fixed _COLLECTIONS tuple below, never from user input.
_CANDIDATE_QUERY_TEMPLATE = "SELECT VALUE c FROM Metadata.`__TABLE__` c;"

_COLLECTIONS: tuple[_Collection, ...] = (
    _Collection("dataset", "Dataset", "DatasetName"),
    _Collection("datatype", "Datatype", "DatatypeName"),
    _Collection("index", "Index", "IndexName"),
    _Collection("function", "Function", "Name"),
    _Collection("synonym", "Synonym", "SynonymName"),
    _Collection("feed", "Feed", "FeedName"),
)


async def run_search_metadata(
    client: CCClient,
    settings: Settings,
    *,
    query: str,
    limit: int = DEFAULT_LIMIT,
) -> ToolResult:
    """Fuzzy-search metadata object names and return the closest matches."""
    needle = query.strip()
    if not needle:
        return ToolResult.error(
            GatewayError(ErrorType.INVALID_PARAMETER, "Provide a non-empty search query.")
        )
    if len(needle) > MAX_QUERY_LEN:
        return ToolResult.error(
            GatewayError(
                ErrorType.INVALID_PARAMETER,
                f"Search query is too long (max {MAX_QUERY_LEN} characters).",
            )
        )
    limit = min(max(limit, 1), MAX_LIMIT)
    ccid = make_client_context_id(settings.agent_session_id, "search_metadata")

    candidate_lists = await asyncio.gather(
        *(_candidates(client, ccid, coll) for coll in _COLLECTIONS)
    )
    scored: list[dict[str, Any]] = []
    needle_lower = needle.lower()
    for candidates in candidate_lists:
        for candidate in candidates:
            score = _score(needle_lower, candidate["name"])
            if score > 0:
                scored.append({**candidate, "score": score})

    scored.sort(key=lambda c: (-c["score"], c["kind"], str(c["name"])))
    window = scored[:limit]
    structured = {
        "status": "success",
        "query": needle,
        "totalMatches": len(scored),
        "limit": limit,
        "matches": window,
    }
    return ToolResult(
        text=f"{len(window)} match(es) for {needle!r} across the metadata catalog.",
        structured=structured,
    )


async def _candidates(
    client: CCClient, ccid: str, collection: _Collection
) -> list[dict[str, Any]]:
    """Fetch (kind, dataverse, name[, dataset]) candidates from one collection."""
    query = _CANDIDATE_QUERY_TEMPLATE.replace("__TABLE__", collection.table)
    try:
        envelope = await client.execute(query, client_context_id=ccid)
    except GatewayError:
        return []
    out: list[dict[str, Any]] = []
    for row in envelope.get("results") or []:
        if not isinstance(row, dict):
            continue
        name = row.get(collection.name_field)
        if not isinstance(name, str):
            continue
        candidate: dict[str, Any] = {
            "kind": collection.kind,
            "dataverse": row.get("DataverseName"),
            "name": name,
        }
        if collection.kind == "index":
            candidate["dataset"] = row.get("DatasetName")
        out.append(candidate)
    return out


def _score(needle_lower: str, name: str) -> int:
    """Fuzzy score: exact > prefix > substring > character-similarity tail."""
    name_lower = name.lower()
    if name_lower == needle_lower:
        return _EXACT_SCORE
    if name_lower.startswith(needle_lower):
        return _PREFIX_SCORE
    if needle_lower in name_lower:
        return _SUBSTRING_SCORE
    ratio = SequenceMatcher(None, needle_lower, name_lower).ratio()
    return int(ratio * _SIMILARITY_WEIGHT)
