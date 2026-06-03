"""asterixdb://cluster/diagnostics: per-node operational health.

Wraps the CC ``/admin/diagnostics`` endpoint (per-node heap, GC, threads, disk,
thread dumps). Returned largely as-is — the CC owns the shape — so an operator or
agent can roll up cluster health in one read.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient


async def read_cluster_diagnostics(client: CCClient) -> dict[str, Any]:
    """Return the CC diagnostics payload (per-node JVM/disk/thread stats)."""
    return await client.admin_diagnostics()
