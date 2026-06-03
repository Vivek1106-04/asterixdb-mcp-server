"""list_dataverses: enumerate the dataverses on the cluster.

The top-level discovery primitive. A tool-driven LLM never reads passive
resources on its own, so dataverse orientation is exposed as a tool (mirroring
list_datasets) and not only as the asterixdb://dataverses resource. Without it a
model has no way to learn which dataverses exist before drilling into datasets.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import GatewayError
from . import ToolResult

_QUERY = "SELECT VALUE dv FROM Metadata.`Dataverse` dv ORDER BY dv.DataverseName;"


async def run_list_dataverses(client: CCClient, settings: Settings) -> ToolResult:
    """List every dataverse on the cluster with its data format."""
    ccid = make_client_context_id(settings.agent_session_id, "list_dataverses")
    try:
        envelope = await client.execute(_QUERY, client_context_id=ccid)
    except GatewayError as err:
        return ToolResult.error(err)

    rows = [r for r in (envelope.get("results") or []) if isinstance(r, dict)]
    dataverses = [
        {"dataverse": r.get("DataverseName"), "dataFormat": r.get("DataFormat")} for r in rows
    ]
    structured: dict[str, Any] = {
        "status": "success",
        "count": len(dataverses),
        "dataverses": dataverses,
    }
    names = ", ".join(str(d["dataverse"]) for d in dataverses) or "none"
    return ToolResult(text=f"{len(dataverses)} dataverse(s): {names}.", structured=structured)
