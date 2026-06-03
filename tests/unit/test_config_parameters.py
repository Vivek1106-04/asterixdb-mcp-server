"""Unit tests for the config-parameters resource."""

from __future__ import annotations

from asterixdb_mcp.config import Settings
from asterixdb_mcp.resources.config_parameters import read_config_parameters


def test_exposes_compiler_allowlist() -> None:
    payload = read_config_parameters(Settings())
    names = {p["name"] for p in payload["compilerParameters"]}
    assert "compiler.parallelism" in names
    assert "compiler.cbo" in names


def test_reflects_configured_limits() -> None:
    settings = Settings(max_time_ms=12_345, max_wait_ms=5_000)
    payload = read_config_parameters(settings)
    assert payload["limits"]["maxTimeMs"] == 12_345
    assert payload["limits"]["maxWaitMs"] == 5_000


def test_reflects_configured_concurrency() -> None:
    settings = Settings(sync_permits=7, async_permits=3, max_concurrent_waits=9)
    payload = read_config_parameters(settings)
    assert payload["concurrency"] == {
        "syncPermits": 7,
        "asyncPermits": 3,
        "maxConcurrentWaits": 9,
    }
