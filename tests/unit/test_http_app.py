"""Unit tests for the HTTP transport app: health probe, bearer auth, build wiring."""

from __future__ import annotations

import logging

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from asterixdb_mcp import server as server_module
from asterixdb_mcp.config import HEALTH_PATH, Settings
from asterixdb_mcp.http_app import (
    BearerAuthMiddleware,
    build_http_app,
    health,
    is_authorized,
)

_TOKEN = "topsecrettoken12345"


def test_is_authorized_variants() -> None:
    assert is_authorized("Bearer " + _TOKEN, _TOKEN) is True
    assert is_authorized("bearer " + _TOKEN, _TOKEN) is True  # scheme case-insensitive
    assert is_authorized(None, _TOKEN) is False
    assert is_authorized("", _TOKEN) is False
    assert is_authorized("Bearer ", _TOKEN) is False  # empty token
    assert is_authorized("Basic " + _TOKEN, _TOKEN) is False  # wrong scheme
    assert is_authorized("Bearer wrong", _TOKEN) is False


async def _protected(_request: object) -> PlainTextResponse:
    return PlainTextResponse("secret")


def _bearer_app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/mcp", _protected, methods=["GET"]),
            Route(HEALTH_PATH, health, methods=["GET"]),
        ]
    )
    app.add_middleware(BearerAuthMiddleware, api_key=_TOKEN, exempt_paths=frozenset({HEALTH_PATH}))
    return app


def test_bearer_middleware_rejects_missing_token() -> None:
    with TestClient(_bearer_app()) as client:  # context runs lifespan (non-http scope)
        resp = client.get("/mcp")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_bearer_middleware_rejects_wrong_token() -> None:
    with TestClient(_bearer_app()) as client:
        resp = client.get("/mcp", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_bearer_middleware_allows_valid_token() -> None:
    with TestClient(_bearer_app()) as client:
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status_code == 200
    assert resp.text == "secret"


def test_health_is_exempt_and_minimal() -> None:
    with TestClient(_bearer_app()) as client:
        resp = client.get(HEALTH_PATH)  # no token
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}  # no version leak


# build_http_app wiring across auth modes


def test_build_http_app_bearer_protects_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(transport="http", auth_mode="bearer", api_key=_TOKEN)
    app = build_http_app(server_module.build_server(settings), settings)
    with TestClient(app) as client:
        assert client.get(HEALTH_PATH).status_code == 200
        assert client.get("/mcp").status_code == 401


def test_build_http_app_none_warns_and_serves_health(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(transport="http", http_host="127.0.0.1", auth_mode="none")
    with caplog.at_level(logging.WARNING):
        app = build_http_app(server_module.build_server(settings), settings)
    assert any("WITHOUT authentication" in r.message for r in caplog.records)
    with TestClient(app) as client:
        assert client.get(HEALTH_PATH).status_code == 200


def _artifact_app(tmp_path: object, **overrides: object) -> tuple[object, str]:
    """Build an http app with a single saved artifact; return (app, artifact_id)."""
    from asterixdb_mcp.artifacts import write_overflow_artifact

    settings = Settings(
        transport="http",
        auth_mode="bearer",
        api_key=_TOKEN,
        artifacts_dir=str(tmp_path),
        **overrides,
    )
    ref = write_overflow_artifact([{"i": 1}, {"i": 2}], settings=settings)
    assert ref is not None
    return build_http_app(server_module.build_server(settings), settings), ref.artifact_id


def test_artifact_download_serves_file_with_token(tmp_path: object) -> None:
    app, artifact_id = _artifact_app(tmp_path)
    auth = {"Authorization": f"Bearer {_TOKEN}"}
    with TestClient(app) as client:
        resp = client.get(f"/artifacts/{artifact_id}", headers=auth)
    assert resp.status_code == 200
    assert resp.json() == [{"i": 1}, {"i": 2}]
    assert "attachment" in resp.headers["content-disposition"]


def test_artifact_download_requires_auth(tmp_path: object) -> None:
    app, artifact_id = _artifact_app(tmp_path)
    with TestClient(app) as client:
        assert client.get(f"/artifacts/{artifact_id}").status_code == 401


def test_artifact_download_unknown_id_is_404(tmp_path: object) -> None:
    app, _ = _artifact_app(tmp_path)
    with TestClient(app) as client:
        resp = client.get("/artifacts/" + "0" * 32, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status_code == 404


def test_build_http_app_oauth_protects_mcp() -> None:
    settings = Settings(
        transport="http",
        auth_mode="oauth",
        oauth_issuer="https://as.example.com",
        oauth_audience="https://mcp.example.com/mcp",
        oauth_jwks_uri="https://as.example.com/jwks",
        oauth_required_scopes=["asterixdb.read"],
    )
    app = build_http_app(server_module.build_server(settings), settings)
    with TestClient(app) as client:
        assert client.get(HEALTH_PATH).status_code == 200
        # No token -> rejected by the SDK's resource-server auth (no JWKS fetch).
        assert client.get("/mcp").status_code == 401
