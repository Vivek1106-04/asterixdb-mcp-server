"""Shared reads of the secondary-index catalog (``Metadata.Index``).

Three tools reason about secondary indexes — ``check_index_usage`` (does one
query use them), ``database_health_check`` (are any duplicate/redundant), and
``recommend_indexes`` (which filtered fields lack one). They all need the same
two primitives: a normalized view of an index's key fields, and a fetch of a
dataset's (or the cluster's) secondary indexes. Centralizing them here keeps the
SearchKey normalization — the one fiddly bit, where a key entry is itself a list
of path segments — defined once.

Read-only by construction: the only statement issued is a ``SELECT`` over the
metadata catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cc_client import CCClient
from .errors import GatewayError

# Secondary indexes only; the primary index is implicit and never recommended on
# or reported against.
ALL_SECONDARY_INDEXES_QUERY = (
    "SELECT VALUE i FROM Metadata.`Index` i WHERE i.IsPrimary = false;"
)


@dataclass(frozen=True)
class SecondaryIndex:
    """A normalized secondary-index record from the metadata catalog."""

    dataverse: str | None
    dataset: str | None
    name: str | None
    structure: str
    key_fields: tuple[str, ...]


def normalize_search_key(search_key: Any) -> list[str]:
    """Render an index SearchKey to a list of dotted field paths.

    Each key entry is itself a list of path segments (e.g. ``[["address","city"]]``
    -> ``"address.city"``); a bare string is taken verbatim. Anything that is not
    a list yields no keys.
    """
    if not isinstance(search_key, list):
        return []
    out: list[str] = []
    for entry in search_key:
        if isinstance(entry, list):
            out.append(".".join(str(seg) for seg in entry))
        else:
            out.append(str(entry))
    return out


def parse_index_row(row: Any) -> SecondaryIndex | None:
    """Turn one raw ``Metadata.Index`` record into a SecondaryIndex.

    Returns None for a row that is not a dict or carries no index name, so a
    malformed catalog row is skipped rather than crashing a scan.
    """
    if not isinstance(row, dict) or not isinstance(row.get("IndexName"), str):
        return None
    return SecondaryIndex(
        dataverse=row.get("DataverseName"),
        dataset=row.get("DatasetName"),
        name=row.get("IndexName"),
        structure=str(row.get("IndexStructure", "")),
        key_fields=tuple(normalize_search_key(row.get("SearchKey"))),
    )


async def fetch_secondary_indexes(
    client: CCClient, ccid: str, *, dataverse: str | None = None
) -> list[SecondaryIndex]:
    """Fetch every secondary index, optionally scoped to one dataverse.

    Degrades to an empty list on a transport/query failure so a caller can treat
    "no indexes known" as a safe default rather than aborting.
    """
    statement = ALL_SECONDARY_INDEXES_QUERY
    parameters: dict[str, Any] | None = None
    if dataverse is not None:
        statement = (
            "SELECT VALUE i FROM Metadata.`Index` i "
            "WHERE i.IsPrimary = false AND i.DataverseName = $dv;"
        )
        parameters = {"dv": dataverse}
    try:
        envelope = await client.execute(
            statement, client_context_id=ccid, statement_parameters=parameters
        )
    except GatewayError:
        return []
    indexes: list[SecondaryIndex] = []
    for row in envelope.get("results") or []:
        parsed = parse_index_row(row)
        if parsed is not None:
            indexes.append(parsed)
    return indexes
