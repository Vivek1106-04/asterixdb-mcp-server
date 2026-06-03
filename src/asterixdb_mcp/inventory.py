"""Catalog inventory: the dataverses and datasets known to Metadata.

Several features (name resolution, corrective errors, dataverse description)
need the live list of dataset names. This module centralizes the single
Metadata.`Dataset` query and the pure extraction of names from the rows.
"""

from __future__ import annotations

from typing import Any

from .cc_client import CCClient

_INVENTORY_QUERY = (
    "SELECT VALUE d FROM Metadata.`Dataset` d ORDER BY d.DataverseName, d.DatasetName;"
)


async def fetch_dataset_rows(client: CCClient, *, ccid: str) -> list[dict[str, Any]]:
    """Fetch every Metadata.`Dataset` record as raw dicts."""
    envelope = await client.execute(_INVENTORY_QUERY, client_context_id=ccid)
    rows = envelope.get("results") or []
    return [r for r in rows if isinstance(r, dict)]


def dataverse_names(rows: list[dict[str, Any]]) -> list[str]:
    """Distinct dataverse names from inventory rows, in first-seen order."""
    seen: list[str] = []
    for row in rows:
        name = row.get("DataverseName")
        if isinstance(name, str) and name not in seen:
            seen.append(name)
    return seen


def dataset_names(rows: list[dict[str, Any]], dataverse: str) -> list[str]:
    """Dataset names within a specific dataverse."""
    out: list[str] = []
    for row in rows:
        if row.get("DataverseName") != dataverse:
            continue
        name = row.get("DatasetName")
        if isinstance(name, str):
            out.append(name)
    return out
