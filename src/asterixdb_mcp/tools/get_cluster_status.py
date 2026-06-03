"""get_cluster_status: cluster version, state, and node roster in one call.

A tool-driven LLM never reads passive resources, so cluster orientation that was
only exposed through the asterixdb://version and asterixdb://cluster/status
resources is surfaced here as a tool. It answers "what version / state / nodes"
directly and, crucially, returns the node ids that get_node_details needs — a
model has no other way to learn them.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..errors import GatewayError
from . import ToolResult


async def run_get_cluster_status(client: CCClient) -> ToolResult:
    """Return AsterixDB version, cluster state, and the per-node roster."""
    try:
        version = await client.admin_version()
        cluster = await client.admin_cluster()
    except GatewayError as err:
        return ToolResult.error(err)

    nodes = _node_summaries(cluster.get("ncs"))
    structured: dict[str, Any] = {
        "status": "success",
        "version": version.get("Git revision") or version.get("version"),
        "state": cluster.get("state"),
        "nodeCount": len(nodes),
        "nodes": nodes,
    }
    node_ids = ", ".join(str(n["nodeId"]) for n in nodes) or "none"
    return ToolResult(
        text=(
            f"Cluster state {structured['state']!r}, {len(nodes)} node(s): {node_ids}. "
            "Pass a nodeId to get_node_details for per-node stats."
        ),
        structured=structured,
    )


def _node_summaries(ncs: Any) -> list[dict[str, Any]]:
    """Flatten the CC ``ncs`` array into a compact per-node roster."""
    if not isinstance(ncs, list):
        return []
    summaries: list[dict[str, Any]] = []
    for entry in ncs:
        if not isinstance(entry, dict):
            continue
        summaries.append(
            {
                "nodeId": entry.get("node_id") or entry.get("nodeId"),
                "state": entry.get("state"),
            }
        )
    return summaries
