"""Unit tests for the concurrency permit pools."""

from __future__ import annotations

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType, GatewayError
from asterixdb_mcp.permits import PermitPool, PermitPools

pytestmark = pytest.mark.anyio


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="capacity must be >= 1"):
        PermitPool(0, "test")


async def test_acquire_and_release_round_trips_in_use() -> None:
    pool = PermitPool(2, "test")
    assert pool.in_use == 0
    assert pool.available == 2

    async with pool.acquire():
        assert pool.in_use == 1
        assert pool.available == 1

    assert pool.in_use == 0
    assert pool.available == 2


async def test_acquire_releases_on_exception_in_body() -> None:
    pool = PermitPool(1, "test")

    with pytest.raises(RuntimeError):
        async with pool.acquire():
            raise RuntimeError("boom")

    assert pool.in_use == 0


async def test_full_pool_raises_not_ready() -> None:
    pool = PermitPool(1, "synchronous query")

    async with pool.acquire():
        # Drive __aenter__ directly: it raises before any body would run, so
        # there is no unreachable suite to mark with a coverage pragma.
        busy = pool.acquire()
        with pytest.raises(GatewayError) as exc_info:
            await busy.__aenter__()

    assert exc_info.value.error_type is ErrorType.NOT_READY
    assert exc_info.value.retryable is True
    assert "synchronous query" in exc_info.value.message


async def test_capacity_is_reusable_after_release() -> None:
    pool = PermitPool(1, "test")

    async with pool.acquire():
        pass
    async with pool.acquire():
        assert pool.in_use == 1


def test_from_settings_sizes_each_pool() -> None:
    settings = Settings(sync_permits=5, async_permits=4, max_concurrent_waits=20)
    pools = PermitPools.from_settings(settings)

    assert pools.sync.capacity == 5
    assert pools.async_.capacity == 4
    assert pools.waits.capacity == 20
