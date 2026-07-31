"""Unit tests for the per-tenant half of the permit pools.

A global ceiling alone does not isolate tenants: one agent holding every permit
leaves every other agent seeing NOT_READY, which is a denial of service against
the gateway's other users even though no limit was exceeded.
"""

from __future__ import annotations

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType, GatewayError
from asterixdb_mcp.permits import PermitPool, PermitPools

pytestmark = pytest.mark.anyio


async def test_one_tenant_cannot_take_the_whole_pool() -> None:
    pool = PermitPool(4, "test", per_principal=2)

    async with pool.acquire("tenant-a"), pool.acquire("tenant-a"):
        with pytest.raises(GatewayError) as excinfo:
            async with pool.acquire("tenant-a"):
                pass

    assert excinfo.value.error_type is ErrorType.NOT_READY


async def test_a_tenant_at_its_own_limit_does_not_block_another() -> None:
    # The point of the per-tenant cap: A's third request is refused, and B's
    # first still succeeds against the same pool.
    pool = PermitPool(4, "test", per_principal=2)

    async with pool.acquire("tenant-a"), pool.acquire("tenant-a"), pool.acquire("tenant-b"):
        assert pool.in_use == 3


async def test_the_global_ceiling_still_binds() -> None:
    # Per-tenant caps do not replace the global limit: the cluster's capacity is
    # finite regardless of how many tenants are asking.
    pool = PermitPool(2, "test", per_principal=2)

    async with pool.acquire("tenant-a"), pool.acquire("tenant-b"):
        with pytest.raises(GatewayError):
            async with pool.acquire("tenant-c"):
                pass


async def test_a_tenants_permits_are_returned_when_its_work_ends() -> None:
    pool = PermitPool(4, "test", per_principal=1)

    async with pool.acquire("tenant-a"):
        pass
    async with pool.acquire("tenant-a"):
        assert pool.in_use == 1


async def test_a_permit_is_returned_even_when_the_body_raises() -> None:
    pool = PermitPool(4, "test", per_principal=1)

    with pytest.raises(RuntimeError):
        async with pool.acquire("tenant-a"):
            raise RuntimeError("boom")

    assert pool.in_use == 0
    assert pool.tracked_principals() == 0


async def test_an_idle_tenant_is_forgotten() -> None:
    # Principals are attacker-chosen strings from a token, so a per-tenant
    # counter that is never reclaimed is itself an unbounded allocation.
    pool = PermitPool(4, "test", per_principal=2)

    for n in range(50):
        async with pool.acquire(f"tenant-{n}"):
            pass

    assert pool.tracked_principals() == 0


async def test_the_refusal_says_which_limit_was_hit() -> None:
    # An agent that cannot tell "you are over your share" from "the gateway is
    # full" cannot back off usefully.
    pool = PermitPool(4, "test", per_principal=1)

    async with pool.acquire("tenant-a"):
        with pytest.raises(GatewayError) as own_limit:
            async with pool.acquire("tenant-a"):
                pass

    full = PermitPool(1, "test", per_principal=4)
    async with full.acquire("tenant-a"):
        with pytest.raises(GatewayError) as global_limit:
            async with full.acquire("tenant-b"):
                pass

    assert "share" in own_limit.value.message
    assert "share" not in global_limit.value.message


async def test_a_cap_at_or_above_capacity_is_no_cap_at_all() -> None:
    # Configuration mistake worth surviving: the per-tenant cap must never be
    # able to exceed the pool it partitions.
    pool = PermitPool(2, "test", per_principal=99)

    async with pool.acquire("tenant-a"), pool.acquire("tenant-a"):
        with pytest.raises(GatewayError):
            async with pool.acquire("tenant-a"):
                pass


def test_pools_take_their_tenant_cap_from_settings() -> None:
    pools = PermitPools.from_settings(Settings(permits_per_principal=1))

    assert pools.sync.per_principal == 1
    assert pools.async_.per_principal == 1
    assert pools.waits.per_principal == 1
