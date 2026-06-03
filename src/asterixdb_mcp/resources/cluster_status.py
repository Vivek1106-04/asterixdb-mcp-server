"""asterixdb://cluster/status: live cluster state resource.

A read-only mirror of GET /admin/cluster: node states, partitions, and the
CC/NC topology. The payload is bounded by the same egress byte ceiling as every
other CC read.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..errors import GatewayError


async def read_cluster_status(client: CCClient) -> dict[str, Any]:
    """Return the CC cluster status, or a degraded payload if the CC is unreachable."""
    try:
        raw = await client.admin_cluster()
    except GatewayError as err:
        return {"reachable": False, "error": err.to_structured()}
    return {
        "reachable": True,
        "state": raw.get("state"),
        "ncs": raw.get("ncs"),
        "cc": raw.get("cc"),
        "raw": raw,
    }
