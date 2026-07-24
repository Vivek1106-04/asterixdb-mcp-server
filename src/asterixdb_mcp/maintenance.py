"""Automatic startup maintenance: bootstrap, catalog walk, and distill.

Runs once when the gateway starts (both transports), making the memory loop
zero-effort for an end user: the store's DDL is created if absent, the OKF
catalog walk keeps concepts in sync with the live schema, and one distill pass
consolidates session events — no scripts, no cron.

Operational guarantees:

- Gated: runs only with ``memory_write_enabled`` AND ``auto_maintenance_enabled``
  (both must be true); read-only deployments are untouched.
- Single-flight: N gateway instances (each agent client spawns its own stdio
  copy) elect one runner via an exclusive lock file created with O_EXCL and
  0600 permissions; a lock older than ``LOCK_STALE_S`` is treated as leaked by
  a crashed process and broken once.
- Bounded: the whole pass is capped by ``MAINTENANCE_TIMEOUT_S`` so a slow or
  absent cluster can never wedge gateway startup.
- Best-effort: every step degrades with a log line; maintenance never raises
  and never blocks serving. A cluster without okf_catalog() simply skips the
  walk.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import httpx

from .cc_client import CCClient
from .config import Settings
from .context_id import make_client_context_id
from .decay import run_decay
from .distill import run_distill
from .errors import GatewayError
from .okf_walk import bootstrap_store, run_walk

logger = logging.getLogger(__name__)

MAINTENANCE_TIMEOUT_S = 60.0
LOCK_STALE_S = 600.0


def _lock_path(settings: Settings) -> Path:
    """Per-cluster lock file in the temp dir; the CC URL hash keys the cluster."""
    key = hashlib.sha256(settings.cc_base_url.encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"asterixdb-mcp-maintenance-{key}.lock"


def acquire_lock(path: Path) -> bool:
    """Take the single-flight lock; break it once if left by a dead process."""
    if _try_create(path):
        return True
    if not _is_stale(path):
        return False
    with contextlib.suppress(OSError):
        path.unlink()
    return _try_create(path)


def _try_create(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    return True


def _is_stale(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime > LOCK_STALE_S
    except OSError:
        # vanished between the failed create and the check: let the retry race
        return True


def release_lock(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


async def run_startup_maintenance(settings: Settings) -> None:
    """One bounded, locked, best-effort maintenance pass. Never raises."""
    if not (
        settings.memory_enabled
        and settings.memory_write_enabled
        and settings.auto_maintenance_enabled
    ):
        return
    lock = _lock_path(settings)
    if not acquire_lock(lock):
        logger.info("startup maintenance skipped: another gateway instance holds the lock")
        return
    try:
        await asyncio.wait_for(_maintenance_pass(settings), MAINTENANCE_TIMEOUT_S)
    # On Python 3.10 wait_for raises asyncio.TimeoutError, a distinct class from
    # the builtin (they merged in 3.11); catch both so the timeout path is hit
    # on every supported version.
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "startup maintenance timed out after %ss; serving anyway", MAINTENANCE_TIMEOUT_S
        )
    except Exception:  # the gateway must start regardless of maintenance health
        logger.exception("startup maintenance failed; serving anyway")
    finally:
        release_lock(lock)


async def _maintenance_pass(settings: Settings) -> None:
    async with httpx.AsyncClient(
        base_url=settings.cc_base_url, timeout=settings.request_timeout_s
    ) as http:
        client = CCClient(settings, http)
        ccid = make_client_context_id(settings.agent_session_id, "maintenance")
        await _step("bootstrap", bootstrap_store(client, ccid))
        await _step("walk", _log_summary("walk", run_walk(client, settings)))
        await _step("decay", _log_summary("decay", run_decay(client, settings)))
        await _step("distill", _log_summary("distill", run_distill(client, settings)))


async def _step(name: str, coro: Coroutine[Any, Any, None]) -> None:
    """Run one maintenance step; a failure is logged and the pass continues."""
    try:
        await coro
    except GatewayError as err:
        logger.warning("maintenance step %r unavailable: %s", name, err.message)


async def _log_summary(name: str, coro: Coroutine[Any, Any, dict[str, int]]) -> None:
    logger.info("maintenance %s: %s", name, await coro)
