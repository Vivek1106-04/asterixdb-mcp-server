"""Unit test for the console-script entry point."""

from __future__ import annotations

import pytest

from asterixdb_mcp import server as server_module
from asterixdb_mcp.config import Settings


def test_main_builds_server_and_runs_over_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: stub settings load, server build, and the blocking run() so the
    # entry point can be exercised without opening a real stdio transport.
    settings = Settings(cc_base_url="http://test-cc:19002")
    calls: dict[str, object] = {}

    class FakeServer:
        def run(self) -> None:
            calls["ran"] = True

    fake_server = FakeServer()

    def fake_load_settings() -> Settings:
        return settings

    def fake_build_server(passed: Settings) -> FakeServer:
        calls["built_with"] = passed
        return fake_server

    monkeypatch.setattr(server_module, "load_settings", fake_load_settings)
    monkeypatch.setattr(server_module, "build_server", fake_build_server)

    # Act
    server_module.main()

    # Assert
    assert calls["built_with"] is settings
    assert calls["ran"] is True
