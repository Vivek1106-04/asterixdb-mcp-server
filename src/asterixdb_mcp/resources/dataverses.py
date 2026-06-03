"""asterixdb://dataverses: a flat list of dataverses for quick orientation.

A cheap first stop for an LLM: which dataverses exist before drilling into
datasets. Reads ``Metadata.Dataverse`` via a read-only SQL++ query.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..context_id import make_client_context_id

_QUERY = "SELECT VALUE dv FROM Metadata.`Dataverse` dv ORDER BY dv.DataverseName;"


async def read_dataverses(client: CCClient, session_id: str) -> dict[str, Any]:
    """Return the list of dataverses with their data format."""
    ccid = make_client_context_id(session_id, "dataverses")
    envelope = await client.execute(_QUERY, client_context_id=ccid)
    rows = [r for r in (envelope.get("results") or []) if isinstance(r, dict)]
    dataverses = [
        {"dataverse": r.get("DataverseName"), "dataFormat": r.get("DataFormat")} for r in rows
    ]
    return {"status": "success", "count": len(dataverses), "dataverses": dataverses}
