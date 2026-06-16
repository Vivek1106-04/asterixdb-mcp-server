"""Unit tests for HTTP transport security wiring and startup validation."""

from __future__ import annotations

import pytest

from asterixdb_mcp.config import MIN_API_KEY_LENGTH, Settings
from asterixdb_mcp.http_security import (
    build_auth,
    build_transport_security,
    is_loopback_host,
    validate_http_security,
)


def test_is_loopback_host() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("example.com")


def test_transport_security_allowlists_own_host_and_loopback() -> None:
    settings = Settings(http_host="0.0.0.0", http_port=19200)
    sec = build_transport_security(settings)
    assert sec.enable_dns_rebinding_protection is True
    assert "0.0.0.0:19200" in sec.allowed_hosts
    assert "127.0.0.1:19200" in sec.allowed_hosts
    assert "localhost:19200" in sec.allowed_hosts
    assert "http://127.0.0.1:19200" in sec.allowed_origins
    assert "https://127.0.0.1:19200" in sec.allowed_origins


def test_transport_security_brackets_ipv6_literals() -> None:
    # The ::1 loopback (always included) and an IPv6 bind host must be bracketed
    # per RFC 3986, both in the host allowlist and in the derived origins.
    settings = Settings(http_host="::1", http_port=19200)
    sec = build_transport_security(settings)
    assert "[::1]:19200" in sec.allowed_hosts
    assert "::1:19200" not in sec.allowed_hosts
    assert "http://[::1]:19200" in sec.allowed_origins
    assert "http://::1:19200" not in sec.allowed_origins


def test_transport_security_includes_configured_extras() -> None:
    settings = Settings(
        http_host="127.0.0.1",
        http_port=19200,
        http_allowed_hosts=["mcp.example.com"],
        http_allowed_origins=["https://app.example.com"],
    )
    sec = build_transport_security(settings)
    assert "mcp.example.com" in sec.allowed_hosts
    assert "https://app.example.com" in sec.allowed_origins
    # An extra allowed Host must NOT implicitly widen the browser-origin allowlist.
    assert "http://mcp.example.com" not in sec.allowed_origins
    assert "https://mcp.example.com" not in sec.allowed_origins


def test_validate_allows_loopback_without_auth() -> None:
    validate_http_security(Settings(http_host="127.0.0.1", auth_mode="none"))


def test_validate_rejects_non_loopback_without_auth() -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        validate_http_security(Settings(http_host="0.0.0.0", auth_mode="none"))


def test_validate_bearer_requires_api_key() -> None:
    with pytest.raises(ValueError, match="requires ASTERIXDB_MCP_API_KEY"):
        validate_http_security(Settings(auth_mode="bearer"))


def test_validate_bearer_rejects_short_api_key() -> None:
    with pytest.raises(ValueError, match="at least"):
        validate_http_security(Settings(auth_mode="bearer", api_key="short"))


def test_validate_bearer_accepts_strong_api_key() -> None:
    validate_http_security(Settings(auth_mode="bearer", api_key="x" * MIN_API_KEY_LENGTH))


def test_validate_oauth_requires_as_config() -> None:
    with pytest.raises(ValueError, match="OAUTH_ISSUER"):
        validate_http_security(Settings(auth_mode="oauth"))


def test_validate_oauth_accepts_full_config() -> None:
    validate_http_security(
        Settings(
            auth_mode="oauth",
            oauth_issuer="https://as.example.com",
            oauth_audience="https://mcp.example.com/mcp",
            oauth_jwks_uri="https://as.example.com/jwks",
        )
    )


def test_validate_oauth_rejects_malformed_url() -> None:
    with pytest.raises(ValueError, match="JWKS_URI must be a valid http"):
        validate_http_security(
            Settings(
                auth_mode="oauth",
                oauth_issuer="https://as.example.com",
                oauth_audience="https://mcp.example.com/mcp",
                oauth_jwks_uri="not-a-url",
            )
        )


def _oauth_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "auth_mode": "oauth",
        "oauth_issuer": "https://as.example.com",
        "oauth_audience": "https://mcp.example.com/mcp",
        "oauth_jwks_uri": "https://as.example.com/jwks",
    }
    base.update(overrides)
    return Settings(**base)


def test_validate_oauth_rejects_empty_algorithms() -> None:
    with pytest.raises(ValueError, match="at least one algorithm"):
        validate_http_security(_oauth_settings(oauth_algorithms=[]))


def test_validate_oauth_rejects_none_algorithm() -> None:
    with pytest.raises(ValueError, match="must not include the 'none'"):
        validate_http_security(_oauth_settings(oauth_algorithms=["RS256", "none"]))


def test_build_auth_none_for_non_oauth() -> None:
    auth, verifier = build_auth(Settings(auth_mode="bearer", api_key="x" * 16))
    assert auth is None
    assert verifier is None


def test_build_auth_for_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    # Avoid constructing a real JWKS client (no network).
    import asterixdb_mcp.http_security as hs

    monkeypatch.setattr(hs, "build_token_verifier", lambda _s: "VERIFIER")
    auth, verifier = build_auth(
        Settings(
            auth_mode="oauth",
            oauth_issuer="https://as.example.com",
            oauth_audience="https://mcp.example.com/mcp",
            oauth_jwks_uri="https://as.example.com/jwks",
            oauth_required_scopes=["asterixdb.read"],
        )
    )
    assert auth is not None
    assert str(auth.issuer_url).rstrip("/") == "https://as.example.com"
    assert auth.required_scopes == ["asterixdb.read"]
    assert verifier == "VERIFIER"
