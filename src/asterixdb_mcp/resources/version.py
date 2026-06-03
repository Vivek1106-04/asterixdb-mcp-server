"""asterixdb://version: lightweight version and liveness resource.

Wraps GET /admin/version and adds the gateway's own version plus the MCP
protocol revision it speaks. Doubles as a cheap liveness probe: a client that
only needs to confirm the cluster is reachable reads this instead of the full
cluster/status payload.
"""

from __future__ import annotations

from typing import Any

from .. import MCP_PROTOCOL_VERSION, __version__
from ..cc_client import CCClient
from ..errors import GatewayError


async def read_version(client: CCClient) -> dict[str, Any]:
    """Return ``{asterixdb: {...}, gateway: {...}}`` or a degraded payload on failure."""
    try:
        raw = await client.admin_version()
    except GatewayError as err:
        return {
            "asterixdb": {"reachable": False, "error": err.to_structured()},
            "gateway": _gateway_block(),
        }
    return {
        "asterixdb": {
            "reachable": True,
            "version": raw.get("Git revision") or raw.get("version"),
            "raw": raw,
        },
        "gateway": _gateway_block(),
    }


def _gateway_block() -> dict[str, str]:
    return {"version": __version__, "protocolVersion": MCP_PROTOCOL_VERSION}
