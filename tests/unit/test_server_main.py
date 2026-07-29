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


def test_main_runs_startup_maintenance_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from asterixdb_mcp import maintenance as maintenance_module

    settings = Settings(cc_base_url="http://test-cc:19002")
    order: list[str] = []

    async def fake_maintenance(passed: Settings) -> None:
        assert passed is settings
        order.append("maintenance")

    class FakeServer:
        def run(self) -> None:
            order.append("serve")

    monkeypatch.setattr(server_module, "load_settings", lambda: settings)
    monkeypatch.setattr(server_module, "build_server", lambda passed: FakeServer())
    monkeypatch.setattr(maintenance_module, "run_startup_maintenance", fake_maintenance)

    server_module.main()
    assert order == ["maintenance", "serve"]


# _client_identity (handshake provenance)


def _ctx_with_client(name: str, version: str = "") -> object:
    """A per-call Context stand-in carrying a REAL handshake params model.

    Only the connection wrapper is faked. ``client_params`` is the SDK's own
    ``InitializeRequestParams`` on purpose: it is the object whose field names the
    identity lookup depends on, and a hand-rolled namespace would keep passing
    after the SDK renamed that field, which is exactly how this broke once.
    """
    from types import SimpleNamespace

    from mcp import types

    params = types.InitializeRequestParams(
        protocolVersion=types.LATEST_PROTOCOL_VERSION,
        capabilities=types.ClientCapabilities(),
        clientInfo=types.Implementation(name=name, version=version),
    )
    return SimpleNamespace(session=SimpleNamespace(client_params=params))


def test_client_identity_reads_handshake_info() -> None:
    from typing import Any, cast

    from asterixdb_mcp.server import _client_identity

    ctx = cast(Any, _ctx_with_client("claude-desktop", "1.2"))
    assert _client_identity(ctx) == "claude-desktop/1.2"


def test_client_identity_handles_missing_pieces() -> None:
    from typing import Any, cast

    from asterixdb_mcp.server import _client_identity

    # No version: name alone. Blank name: None.
    assert _client_identity(cast(Any, _ctx_with_client("antigravity"))) == "antigravity"
    assert _client_identity(cast(Any, _ctx_with_client("  ", "1"))) is None


def test_client_identity_is_none_when_the_handshake_carried_no_client_info() -> None:
    """Absent client info yields no provenance rather than a partial identity.

    Built from a plain namespace, not the SDK params model: the 2.x model makes
    ``clientInfo`` required, so it cannot express this case at all. The guard stays
    because ``client_params`` is only as trustworthy as the peer that filled it.
    """
    from types import SimpleNamespace
    from typing import Any, cast

    from asterixdb_mcp.server import _client_identity

    ctx = SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace()))
    assert _client_identity(cast(Any, ctx)) is None


def test_client_identity_is_none_outside_a_request() -> None:
    """A Context with no live request raises on ``.session``; provenance is additive."""
    from typing import Any, cast

    from asterixdb_mcp.server import _client_identity

    class Detached:
        @property
        def session(self) -> object:
            raise LookupError("no active request context")

    assert _client_identity(cast(Any, Detached())) is None
