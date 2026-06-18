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


@dataclass(frozen=True)
class IndexDetail:
    """A fully-detailed secondary-index record for the resource catalog.

    Where ``SecondaryIndex`` keeps only what the analysis tools need, this carries
    the complete ``Metadata.Index`` picture an agent reads as attached context:
    key field types, enforcement, the record-vs-meta source of each key, array
    ``SearchKeyElements``, n-gram width, and full-text config.
    """

    dataverse: str | None
    dataset: str | None
    name: str | None
    structure: str
    is_primary: bool
    is_enforced: bool
    key_fields: tuple[str, ...]
    key_field_types: tuple[str, ...]
    search_key_source_indicator: tuple[int, ...]
    search_key_elements: Any
    exclude_unknown_key: Any
    gram_length: Any
    full_text_config: Any

    def to_dict(self) -> dict[str, Any]:
        """Lean serialization: always-present identity fields, optionals only when set.

        Optional metadata (``gramLength``, ``fullTextConfig``, ``searchKeyElements``,
        ``excludeUnknownKey``) is emitted only when the catalog carried a value, so a
        plain BTREE index does not ship a wall of nulls.
        """
        out: dict[str, Any] = {
            "dataverse": self.dataverse,
            "dataset": self.dataset,
            "name": self.name,
            "structure": self.structure,
            "isPrimary": self.is_primary,
            "isEnforced": self.is_enforced,
            "keyFields": list(self.key_fields),
        }
        if self.key_field_types:
            out["keyFieldTypes"] = list(self.key_field_types)
        if self.search_key_source_indicator:
            out["searchKeySourceIndicator"] = list(self.search_key_source_indicator)
        for key, value in (
            ("searchKeyElements", self.search_key_elements),
            ("excludeUnknownKey", self.exclude_unknown_key),
            ("gramLength", self.gram_length),
            ("fullTextConfig", self.full_text_config),
        ):
            if value is not None:
                out[key] = value
        return out


def _int_tuple(value: Any) -> tuple[int, ...]:
    """Coerce a metadata list of source indicators to a tuple of ints, skipping non-ints."""
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, int))


def _str_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a metadata list (e.g. SearchKeyType) to a tuple of strings."""
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value)


def parse_index_detail_row(row: Any) -> IndexDetail | None:
    """Turn one raw ``Metadata.Index`` record into a fully-detailed IndexDetail.

    Returns None for a non-dict row or one without an index name, so a malformed
    catalog row is skipped rather than crashing a scan.
    """
    if not isinstance(row, dict) or not isinstance(row.get("IndexName"), str):
        return None
    return IndexDetail(
        dataverse=row.get("DataverseName"),
        dataset=row.get("DatasetName"),
        name=row.get("IndexName"),
        structure=str(row.get("IndexStructure", "")),
        is_primary=bool(row.get("IsPrimary", False)),
        is_enforced=bool(row.get("IsEnforced", False)),
        key_fields=tuple(normalize_search_key(row.get("SearchKey"))),
        key_field_types=_str_tuple(row.get("SearchKeyType")),
        search_key_source_indicator=_int_tuple(row.get("SearchKeySourceIndicator")),
        search_key_elements=row.get("SearchKeyElements"),
        exclude_unknown_key=row.get("ExcludeUnknownKey"),
        gram_length=row.get("GramLength"),
        full_text_config=row.get("FullTextConfig"),
    )


async def fetch_indexes_detailed(
    client: CCClient, ccid: str, *, dataverse: str, dataset: str | None = None
) -> list[IndexDetail]:
    """Fetch fully-detailed secondary indexes for a dataverse, optionally one dataset.

    Degrades to an empty list on a transport/query failure so a resource read
    yields an empty catalog document rather than a protocol-level error.
    """
    statement = (
        "SELECT VALUE i FROM Metadata.`Index` i "
        "WHERE i.IsPrimary = false AND i.DataverseName = $dv"
    )
    parameters: dict[str, Any] = {"dv": dataverse}
    if dataset is not None:
        statement += " AND i.DatasetName = $ds"
        parameters["ds"] = dataset
    statement += ";"
    try:
        envelope = await client.execute(
            statement, client_context_id=ccid, statement_parameters=parameters
        )
    except GatewayError:
        return []
    indexes: list[IndexDetail] = []
    for row in envelope.get("results") or []:
        parsed = parse_index_detail_row(row)
        if parsed is not None:
            indexes.append(parsed)
    return indexes


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
