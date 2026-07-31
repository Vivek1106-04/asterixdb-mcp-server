"""Concurrency permit pools.

The gateway bounds how many queries it will have in flight against the cluster
at once, split by cost profile:

- a sync pool guards blocking ``execute_query`` calls,
- an async pool guards ``submit_async_query`` submissions,
- a waits pool guards the in-gateway long-poll loops of ``wait_on_async_query``.

Acquisition is non-blocking: when a pool is full the gateway applies immediate
backpressure (``NOT_READY``, which is retryable) rather than queueing the caller
and holding a connection open. ``NOT_READY`` is the MCP-tool surface of the
JSON-RPC "server busy" condition (code -32003); a transport that speaks raw
JSON-RPC can map it to that code.

Pools are per-process, in-memory, and reset with the process. They hold no CC
state, so the sidecar stays stateless from the cluster's point of view.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .config import Settings
from .errors import ErrorType, GatewayError

# The MCP-tool error surface of JSON-RPC "server busy". Exposed for a transport
# that wants to translate the gateway's backpressure into a raw JSON-RPC error.
JSONRPC_SERVER_BUSY = -32003

# The bucket for work that arrived on a client with no tenant bound to it. Its
# own bucket rather than a share of somebody else's: an unattributed caller must
# not be able to spend a real tenant's allowance.
UNATTRIBUTED_PRINCIPAL = "unattributed"


class PermitPool:
    """A non-blocking, fixed-capacity concurrency limiter.

    ``acquire`` is an async context manager that takes a permit on entry and
    returns it on exit. If no permit is free it raises immediately instead of
    waiting, so a busy gateway sheds load rather than buffering it.
    """

    def __init__(self, capacity: int, name: str, per_principal: int | None = None) -> None:
        if capacity < 1:
            raise ValueError(f"permit pool {name!r} capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._name = name
        # A share larger than the pool is not a share. Clamping rather than
        # raising keeps a misconfiguration from refusing to start a gateway that
        # would still be correct, just unpartitioned.
        self._per_principal = min(per_principal, capacity) if per_principal else capacity
        self._in_use = 0
        self._held: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def per_principal(self) -> int:
        return self._per_principal

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def available(self) -> int:
        return self._capacity - self._in_use

    def tracked_principals(self) -> int:
        """How many tenants currently hold a permit. Diagnostics and tests."""
        return len(self._held)

    @asynccontextmanager
    async def acquire(self, principal: str | None) -> AsyncIterator[None]:
        """Take a permit for ``principal`` for the duration of the body.

        Both limits are checked: the pool's global capacity, which is what the
        cluster can actually take, and the caller's share of it, which is what
        stops one tenant from making the gateway look full to everyone else.

        Raises:
            GatewayError: NOT_READY when either limit is reached.
        """
        who = principal or UNATTRIBUTED_PRINCIPAL
        await self._take(who)
        try:
            yield
        finally:
            await self._give_back(who)

    async def _take(self, principal: str) -> None:
        async with self._lock:
            if self._in_use >= self._capacity:
                raise GatewayError(
                    ErrorType.NOT_READY,
                    f"The gateway is at capacity for {self._name} work "
                    f"({self._capacity} concurrent). Retry shortly.",
                )
            if self._held.get(principal, 0) >= self._per_principal:
                raise GatewayError(
                    ErrorType.NOT_READY,
                    f"This client is at its share of {self._name} work "
                    f"({self._per_principal} concurrent). Retry shortly, or let "
                    "the queries already in flight finish first.",
                )
            self._in_use += 1
            self._held[principal] = self._held.get(principal, 0) + 1

    async def _give_back(self, principal: str) -> None:
        async with self._lock:
            self._in_use -= 1
            # Dropped at zero rather than left at zero: a principal is a string
            # from a token, so a counter kept per principal seen would be an
            # unbounded allocation keyed by something the caller chooses.
            remaining = self._held.get(principal, 1) - 1
            if remaining > 0:
                self._held[principal] = remaining
            else:
                self._held.pop(principal, None)


@dataclass(frozen=True)
class PermitPools:
    """The gateway's three permit pools, sized from settings."""

    sync: PermitPool
    async_: PermitPool
    waits: PermitPool

    @classmethod
    def from_settings(cls, settings: Settings) -> PermitPools:
        share = settings.permits_per_principal
        return cls(
            sync=PermitPool(settings.sync_permits, "synchronous query", share),
            async_=PermitPool(settings.async_permits, "asynchronous query", share),
            waits=PermitPool(settings.max_concurrent_waits, "result wait", share),
        )
