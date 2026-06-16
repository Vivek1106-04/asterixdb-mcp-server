"""Unit tests for OAuth 2.1 resource-server token verification."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from asterixdb_mcp import auth as auth_module
from asterixdb_mcp.auth import (
    JwtTokenVerifier,
    _as_int,
    _client_id,
    build_token_verifier,
    extract_scopes,
)
from asterixdb_mcp.config import Settings

pytestmark = pytest.mark.anyio

_ISSUER = "https://as.example.com"
_AUDIENCE = "https://mcp.example.com/mcp"


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    """An RSA keypair as (private PEM, public PEM) for signing/verifying test JWTs."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _token(private_pem: str, **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "exp": int(time.time()) + 3600,
        "scope": "asterixdb.read",
        "client_id": "agent-1",
    }
    claims.update(overrides)
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _verifier(public_pem: str, *, required_scopes: list[str] | None = None) -> JwtTokenVerifier:
    return JwtTokenVerifier(
        issuer=_ISSUER,
        audience=_AUDIENCE,
        required_scopes=required_scopes if required_scopes is not None else ["asterixdb.read"],
        algorithms=["RS256"],
        signing_key_resolver=lambda _token: public_pem,
    )


async def test_valid_token_is_accepted(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    result = await _verifier(public_pem).verify_token(_token(private_pem))
    assert result is not None
    assert result.client_id == "agent-1"
    assert result.scopes == ["asterixdb.read"]
    assert result.resource == _AUDIENCE
    assert result.expires_at is not None and result.expires_at > time.time()


async def test_wrong_audience_rejected(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    token = _token(private_pem, aud="https://other.example.com")
    assert await _verifier(public_pem).verify_token(token) is None


async def test_wrong_issuer_rejected(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    token = _token(private_pem, iss="https://evil.example.com")
    assert await _verifier(public_pem).verify_token(token) is None


async def test_expired_token_rejected(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    token = _token(private_pem, exp=int(time.time()) - 10)
    assert await _verifier(public_pem).verify_token(token) is None


async def test_missing_required_scope_rejected(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    token = _token(private_pem, scope="something.else")
    verifier = _verifier(public_pem, required_scopes=["asterixdb.read"])
    assert await verifier.verify_token(token) is None


async def test_scopes_from_scp_list_claim(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    # No `scope` string; scopes arrive as the `scp` list instead.
    token = _token(private_pem, scope=None, scp=["asterixdb.read", "extra"])
    result = await _verifier(public_pem).verify_token(token)
    assert result is not None
    assert result.scopes == ["asterixdb.read", "extra"]


async def test_no_required_scopes_accepts_unscoped_token(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    token = _token(private_pem, scope=None)
    result = await _verifier(public_pem, required_scopes=[]).verify_token(token)
    assert result is not None
    assert result.scopes == []


async def test_malformed_token_rejected(keypair: tuple[str, str]) -> None:
    _private_pem, public_pem = keypair
    assert await _verifier(public_pem).verify_token("not.a.jwt") is None


async def test_resolver_failure_rejected(keypair: tuple[str, str]) -> None:
    private_pem, _public_pem = keypair

    def boom(_token: str) -> Any:
        raise RuntimeError("jwks down")

    verifier = JwtTokenVerifier(
        issuer=_ISSUER,
        audience=_AUDIENCE,
        required_scopes=[],
        algorithms=["RS256"],
        signing_key_resolver=boom,
    )
    assert await verifier.verify_token(_token(private_pem)) is None


def test_extract_scopes_variants() -> None:
    assert extract_scopes({"scope": "a b"}) == {"a", "b"}
    assert extract_scopes({"scp": ["a", "b"]}) == {"a", "b"}
    assert extract_scopes({}) == set()


def test_client_id_fallback_chain() -> None:
    assert _client_id({"client_id": "c", "azp": "z", "sub": "s"}) == "c"
    assert _client_id({"azp": "z", "sub": "s"}) == "z"
    assert _client_id({"sub": "s"}) == "s"
    assert _client_id({}) == ""


def test_as_int_coerces_only_numbers() -> None:
    assert _as_int(5) == 5
    assert _as_int(5.0) == 5
    assert _as_int("nope") is None
    assert _as_int(None) is None


def test_build_token_verifier_wires_jwks_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSigningKey:
        key = "PEM-KEY"

    class FakeJWKClient:
        def __init__(self, uri: str) -> None:
            self.uri = uri

        def get_signing_key_from_jwt(self, token: str) -> FakeSigningKey:
            assert token == "tok"
            return FakeSigningKey()

    monkeypatch.setattr(auth_module.jwt, "PyJWKClient", FakeJWKClient)
    settings = Settings(
        oauth_issuer=_ISSUER,
        oauth_audience=_AUDIENCE,
        oauth_jwks_uri="https://as.example.com/jwks",
        oauth_required_scopes=["asterixdb.read"],
    )
    verifier = build_token_verifier(settings)
    # The wired resolver pulls the signing key from the (faked) JWKS client.
    assert verifier._resolve_key("tok") == "PEM-KEY"
