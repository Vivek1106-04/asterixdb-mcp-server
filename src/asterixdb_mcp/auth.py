"""OAuth 2.1 resource-server token verification.

The gateway is an OAuth 2.1 **resource server**, not an authorization server: it
never issues, refreshes, or stores tokens. It only verifies the bearer access
token presented on each HTTP request against the public keys of an external
authorization server (Auth0, Keycloak, WorkOS, Okta, ...), then enforces issuer,
audience (RFC 8707), expiry, and required scopes.

The signing key is resolved from the AS's JWKS endpoint (``build_token_verifier``
wires a cached ``PyJWKClient``). The verifier itself takes the resolver as a
dependency so it can be unit-tested with a fixed key and no network.

Any verification failure returns ``None`` — the SDK's bearer middleware turns
that into a 401 with a ``WWW-Authenticate`` challenge. Failures are intentionally
opaque to the caller (no detail on *why* a token was rejected) to avoid handing
an attacker an oracle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken

from .config import Settings

# JWT claims required to be present and valid; without these a token is rejected.
_REQUIRED_CLAIMS = ["exp", "iss", "aud"]


class JwtTokenVerifier:
    """Verify a bearer access token as a JWT signed by the configured AS."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        required_scopes: list[str],
        algorithms: list[str],
        signing_key_resolver: Callable[[str], Any],
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._required_scopes = frozenset(required_scopes)
        self._algorithms = algorithms
        self._resolve_key = signing_key_resolver

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the decoded access token if valid and sufficiently scoped, else None."""
        try:
            key = self._resolve_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": _REQUIRED_CLAIMS},
            )
        except Exception:  # any decode/signature/expiry/JWKS failure is a 401, not a 500
            return None

        scopes = extract_scopes(claims)
        if not self._required_scopes.issubset(scopes):
            return None

        return AccessToken(
            token=token,
            client_id=_client_id(claims),
            scopes=sorted(scopes),
            expires_at=_as_int(claims.get("exp")),
            resource=self._audience,
        )


def extract_scopes(claims: dict[str, Any]) -> set[str]:
    """Read scopes from the OAuth ``scope`` string or the ``scp`` list claim."""
    raw = claims.get("scope")
    if isinstance(raw, str):
        return set(raw.split())
    scp = claims.get("scp")
    if isinstance(scp, list):
        return {str(item) for item in scp}
    return set()


def _client_id(claims: dict[str, Any]) -> str:
    """Best-effort caller identity: client_id, else authorized party, else subject."""
    return str(claims.get("client_id") or claims.get("azp") or claims.get("sub") or "")


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def build_token_verifier(settings: Settings) -> JwtTokenVerifier:
    """Build the production verifier, resolving signing keys from the AS JWKS (cached)."""
    from jwt import PyJWKClient

    client = PyJWKClient(settings.oauth_jwks_uri)

    def resolve(token: str) -> Any:
        return client.get_signing_key_from_jwt(token).key

    return JwtTokenVerifier(
        issuer=settings.oauth_issuer,
        audience=settings.oauth_audience,
        required_scopes=settings.oauth_required_scopes,
        algorithms=settings.oauth_algorithms,
        signing_key_resolver=resolve,
    )
