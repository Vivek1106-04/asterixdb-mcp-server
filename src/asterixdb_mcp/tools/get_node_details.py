"""get_node_details: per-NC drill-down.

Wraps the CC ``/admin/cluster/node/{nodeId}`` endpoint into an LLM-friendly
result. Use it after get_cluster_status has revealed node ids.

Defense-in-Depth:
- Layer 1: the schema says to call get_cluster_status first to get valid
  node names.
- Layer 2: the node id is validated against an identifier pattern BEFORE building
  the URL, so a slash or whitespace can never traverse to another admin path; an
  unknown node returns a self-correcting NOT_FOUND.
"""

from __future__ import annotations

import re
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..errors import ErrorType, GatewayError
from . import ToolResult

# Node ids are simple identifiers (letters, digits, _, -, .). Anything else could
# alter the admin path, so it is rejected before the request is built.
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


async def run_get_node_details(
    client: CCClient,
    settings: Settings,
    *,
    node: str,
) -> ToolResult:
    """Return per-node CC statistics for a single node controller."""
    node_id = node.strip()
    if not node_id:
        return ToolResult.error(
            GatewayError(ErrorType.INVALID_PARAMETER, "Provide a node id.")
        )
    if not _NODE_ID_RE.match(node_id):
        return ToolResult.error(
            GatewayError(
                ErrorType.INVALID_PARAMETER,
                f"Invalid node id {node_id!r}. Node ids contain only letters, digits, "
                "'_', '-', and '.'. Call get_cluster_status for valid node ids.",
            )
        )

    try:
        detail = await client.admin_node_detail(node_id)
    except GatewayError as err:
        # A 404 surfaces from the CC as a non-JSON body; reframe it as NOT_FOUND.
        if err.error_type in (ErrorType.INTERNAL, ErrorType.NOT_FOUND):
            return ToolResult.error(
                GatewayError(
                    ErrorType.NOT_FOUND,
                    f"No node named {node_id!r} was found. Call get_cluster_status "
                    "for the list of node ids.",
                )
            )
        return ToolResult.error(err)

    structured: dict[str, Any] = {"status": "success", "node": node_id, "details": detail}
    return ToolResult(text=f"Node {node_id} statistics retrieved.", structured=structured)
