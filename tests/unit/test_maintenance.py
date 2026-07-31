"""Unit tests for automatic startup maintenance (lock, gating, degradation)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from asterixdb_mcp import maintenance
from asterixdb_mcp.config import Settings
from asterixdb_mcp.maintenance import (
    acquire_lock,
    release_lock,
    run_startup_maintenance,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "maintenance.lock"
    monkeypatch.setattr(maintenance, "_lock_path", lambda settings: path)
    return path


# lock semantics


def test_lock_is_exclusive_and_owner_scoped(lock: Path) -> None:
    assert acquire_lock(lock) is True
    assert oct(lock.stat().st_mode & 0o777) == "0o600"
    assert json.loads(lock.read_text())["pid"] == os.getpid()
    assert acquire_lock(lock) is False  # second taker loses
    release_lock(lock)
    assert not lock.exists()
    release_lock(lock)  # idempotent


def test_stale_lock_is_broken_once(lock: Path) -> None:
    lock.write_text(json.dumps({"pid": 0, "ts": 0}))
    old = time.time() - maintenance.LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    assert acquire_lock(lock) is True  # stale lock replaced
    assert json.loads(lock.read_text())["pid"] == os.getpid()


def test_fresh_foreign_lock_is_respected(lock: Path) -> None:
    lock.write_text(json.dumps({"pid": 0, "ts": time.time()}))
    assert acquire_lock(lock) is False


# run_startup_maintenance


def _enabled_settings() -> Settings:
    return Settings(
        cc_base_url="http://test-cc:19002",
        agent_session_id="sess-test",
        memory_write_enabled=True,
    )


async def test_disabled_gateways_do_nothing(lock: Path) -> None:
    await run_startup_maintenance(Settings())  # memory writes off
    await run_startup_maintenance(
        Settings(memory_write_enabled=True, auto_maintenance_enabled=False)
    )
    assert not lock.exists()


async def test_pass_runs_all_steps_and_releases_lock(
    lock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    async def fake_pass(settings: Settings) -> None:
        ran.append("pass")
        assert lock.exists()  # held while running

    monkeypatch.setattr(maintenance, "_maintenance_pass", fake_pass)
    await run_startup_maintenance(_enabled_settings())
    assert ran == ["pass"]
    assert not lock.exists()


async def test_contended_lock_skips_the_pass(lock: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock.write_text(json.dumps({"pid": 0, "ts": time.time()}))

    async def fail_pass(settings: Settings) -> None:  # pragma: no cover - must not run
        raise AssertionError("pass ran despite held lock")

    monkeypatch.setattr(maintenance, "_maintenance_pass", fail_pass)
    await run_startup_maintenance(_enabled_settings())
    assert lock.exists()  # the foreign lock is left alone


async def test_pass_failure_is_swallowed_and_lock_released(
    lock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_pass(settings: Settings) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(maintenance, "_maintenance_pass", broken_pass)
    await run_startup_maintenance(_enabled_settings())  # must not raise
    assert not lock.exists()


async def test_timeout_is_bounded_and_swallowed(
    lock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def slow_pass(settings: Settings) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(maintenance, "_maintenance_pass", slow_pass)
    monkeypatch.setattr(maintenance, "MAINTENANCE_TIMEOUT_S", 0.01)
    await run_startup_maintenance(_enabled_settings())  # returns promptly, no raise
    assert not lock.exists()


async def test_steps_degrade_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    # bootstrap and walk fail with GatewayError (e.g. no okf_catalog on the
    # cluster); decay and distill must still run.
    from asterixdb_mcp.errors import ErrorType, GatewayError

    order: list[str] = []

    async def failing(*args: object, **kwargs: object) -> None:
        order.append("failing")
        raise GatewayError(ErrorType.INTERNAL, "unavailable")

    async def ok_backfill(*args: object, **kwargs: object) -> dict[str, int]:
        order.append("backfill")
        return {"concepts": 0, "events": 0}

    async def ok_flush(*args: object, **kwargs: object) -> None:
        order.append("flush")

    async def ok_revalidate(*args: object, **kwargs: object) -> dict[str, int]:
        order.append("revalidate")
        return {"checked": 0, "retired": 0, "unprovable": 0}

    async def ok_decay(*args: object, **kwargs: object) -> dict[str, int]:
        order.append("decay")
        return {"candidates": 0, "archived": 0}

    async def ok_distill(*args: object, **kwargs: object) -> dict[str, int]:
        order.append("distill")
        return {"events": 0}

    # Every step is stubbed, including the ones expected to succeed: an
    # unstubbed step would reach for the network, and what it does when the
    # host does not resolve is the environment's business, not this test's.
    monkeypatch.setattr(maintenance, "bootstrap_store", failing)
    monkeypatch.setattr(maintenance, "backfill_principals", ok_backfill)
    monkeypatch.setattr(maintenance, "flush_buffered_events", ok_flush)
    monkeypatch.setattr(maintenance, "run_walk", failing)
    monkeypatch.setattr(maintenance, "run_revalidation", ok_revalidate)
    monkeypatch.setattr(maintenance, "run_decay", ok_decay)
    monkeypatch.setattr(maintenance, "run_distill", ok_distill)
    await maintenance._maintenance_pass(_enabled_settings())
    assert order == [
        "failing",
        "backfill",
        "flush",
        "failing",
        "revalidate",
        "decay",
        "distill",
    ]


def test_lock_path_is_temp_scoped_and_cluster_keyed() -> None:
    a = maintenance._lock_path(Settings(cc_base_url="http://cc-a:19002"))
    b = maintenance._lock_path(Settings(cc_base_url="http://cc-b:19002"))
    assert a != b  # different clusters never share a lock
    assert a.name.startswith("asterixdb-mcp-maintenance-")


def test_stale_lock_unlink_race_loses_gracefully(
    lock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock.write_text("{}")
    old = time.time() - maintenance.LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    monkeypatch.setattr(Path, "unlink", lambda self: (_ for _ in ()).throw(OSError("gone")))
    assert acquire_lock(lock) is False  # cannot break -> concede


def test_stale_check_treats_vanished_lock_as_stale(
    lock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First create fails (exists), stat then fails (vanished): retry runs and
    # the second create succeeds.
    lock.write_text("{}")
    real_stat = Path.stat
    calls = {"n": 0}

    def racy_stat(self: Path, **kwargs: object):
        if self == lock and calls["n"] == 0:
            calls["n"] += 1
            self.unlink()
            raise FileNotFoundError(self)
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", racy_stat)
    assert acquire_lock(lock) is True
