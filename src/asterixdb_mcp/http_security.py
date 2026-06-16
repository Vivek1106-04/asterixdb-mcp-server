"""HTTP transport security: DNS-rebinding allowlist, startup checks, auth wiring.

Three concerns, kept out of ``server.py`` so the MCP surface stays its single
responsibility:

1. ``build_transport_security`` — DNS-rebinding protection. A browser page open on
   a victim's machine can ``fetch()`` a localhost MCP server; without Host/Origin
   validation it could drive the tools. The MCP spec mandates validating these
   headers for local HTTP servers. We allow only the gateway's own host:port (plus
   loopback aliases) and any explicitly configured proxy host/origin.

2. ``validate_http_security`` — fail fast on an unsafe configuration instead of
   silently exposing the database: refuse a non-loopback bind with no auth, a
   bearer mode with a missing/short token, or an oauth mode missing its AS config.

3. ``build_auth`` — assemble the FastMCP resource-server auth settings and token
   verifier for oauth mode.
"""

from __future__ import annotations

from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, TypeAdapter

from .auth import build_token_verifier
from .config import MIN_API_KEY_LENGTH, Settings

# Hosts treated as loopback for the no-auth bind check and the allowlist.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# Coerces a configured URL string into the AnyHttpUrl that AuthSettings expects.
_HTTP_URL = TypeAdapter(AnyHttpUrl)


def is_loopback_host(host: str) -> bool:
    """Whether a bind host is loopback (safe to serve without authentication)."""
    return host in _LOOPBACK_HOSTS


def build_transport_security(settings: Settings) -> TransportSecuritySettings:
    """Allowlist the gateway's own host:port (and loopback) for DNS-rebinding checks."""
    port = settings.http_port
    hosts = {f"{host}:{port}" for host in (settings.http_host, *_LOOPBACK_HOSTS)}
    hosts.update(settings.http_allowed_hosts)
    origins = {f"{scheme}://{host}" for host in hosts for scheme in ("http", "https")}
    origins.update(settings.http_allowed_origins)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


def validate_http_security(settings: Settings) -> None:
    """Raise ValueError on an unsafe HTTP configuration; return None when safe."""
    if settings.auth_mode == "none" and not is_loopback_host(settings.http_host):
        raise ValueError(
            f"Refusing to serve HTTP on non-loopback host {settings.http_host!r} with "
            "auth_mode='none'. Set ASTERIXDB_MCP_AUTH_MODE to 'bearer' or 'oauth', "
            "or bind a loopback host."
        )
    if settings.auth_mode == "bearer":
        _validate_bearer(settings)
    if settings.auth_mode == "oauth":
        _validate_oauth(settings)


def build_auth(settings: Settings) -> tuple[AuthSettings | None, TokenVerifier | None]:
    """Return (auth settings, verifier) for oauth mode; (None, None) otherwise."""
    if settings.auth_mode != "oauth":
        return None, None
    auth = AuthSettings(
        issuer_url=_HTTP_URL.validate_python(settings.oauth_issuer),
        resource_server_url=_HTTP_URL.validate_python(settings.oauth_audience),
        required_scopes=settings.oauth_required_scopes,
    )
    return auth, build_token_verifier(settings)


def _validate_bearer(settings: Settings) -> None:
    if not settings.api_key:
        raise ValueError("auth_mode='bearer' requires ASTERIXDB_MCP_API_KEY to be set.")
    if len(settings.api_key) < MIN_API_KEY_LENGTH:
        raise ValueError(f"ASTERIXDB_MCP_API_KEY must be at least {MIN_API_KEY_LENGTH} characters.")


def _validate_oauth(settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("ASTERIXDB_MCP_OAUTH_ISSUER", settings.oauth_issuer),
            ("ASTERIXDB_MCP_OAUTH_AUDIENCE", settings.oauth_audience),
            ("ASTERIXDB_MCP_OAUTH_JWKS_URI", settings.oauth_jwks_uri),
        )
        if not value
    ]
    if missing:
        raise ValueError("auth_mode='oauth' requires these settings: " + ", ".join(missing))
