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


def test_main_serves_http_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # transport='http' routes through _serve_http instead of the stdio run loop.
    settings = Settings(cc_base_url="http://test-cc:19002", transport="http", auth_mode="none")
    fake_server = object()
    served: dict[str, object] = {}

    monkeypatch.setattr(server_module, "load_settings", lambda: settings)
    monkeypatch.setattr(server_module, "build_server", lambda _s: fake_server)
    monkeypatch.setattr(
        server_module,
        "_serve_http",
        lambda srv, s: served.update(server=srv, settings=s),
    )

    server_module.main()

    assert served["server"] is fake_server
    assert served["settings"] is settings


def test_serve_http_runs_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        cc_base_url="http://test-cc:19002",
        transport="http",
        http_host="127.0.0.1",
        http_port=19200,
        auth_mode="none",
    )
    server = server_module.build_server(settings)
    recorded: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int) -> None:
        recorded.update(app=app, host=host, port=port)

    monkeypatch.setattr("uvicorn.run", fake_run)

    server_module._serve_http(server, settings)

    assert recorded["host"] == "127.0.0.1"
    assert recorded["port"] == 19200
    assert recorded["app"] is not None
