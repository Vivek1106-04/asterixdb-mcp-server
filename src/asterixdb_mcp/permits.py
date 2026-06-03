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


class PermitPool:
    """A non-blocking, fixed-capacity concurrency limiter.

    ``acquire`` is an async context manager that takes a permit on entry and
    returns it on exit. If no permit is free it raises immediately instead of
    waiting, so a busy gateway sheds load rather than buffering it.
    """

    def __init__(self, capacity: int, name: str) -> None:
        if capacity < 1:
            raise ValueError(f"permit pool {name!r} capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._name = name
        self._in_use = 0
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def available(self) -> int:
        return self._capacity - self._in_use

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """Take a permit for the duration of the ``async with`` body.

        Raises:
            GatewayError: NOT_READY when the pool is already at capacity.
        """
        await self._take()
        try:
            yield
        finally:
            await self._give_back()

    async def _take(self) -> None:
        async with self._lock:
            if self._in_use >= self._capacity:
                raise GatewayError(
                    ErrorType.NOT_READY,
                    f"The gateway is at capacity for {self._name} work "
                    f"({self._capacity} concurrent). Retry shortly.",
                )
            self._in_use += 1

    async def _give_back(self) -> None:
        async with self._lock:
            self._in_use -= 1


@dataclass(frozen=True)
class PermitPools:
    """The gateway's three permit pools, sized from settings."""

    sync: PermitPool
    async_: PermitPool
    waits: PermitPool

    @classmethod
    def from_settings(cls, settings: Settings) -> PermitPools:
        return cls(
            sync=PermitPool(settings.sync_permits, "synchronous query"),
            async_=PermitPool(settings.async_permits, "asynchronous query"),
            waits=PermitPool(settings.max_concurrent_waits, "result wait"),
        )
