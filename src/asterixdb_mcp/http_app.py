"""Streamable HTTP transport: the ASGI app, a ``/health`` probe, and bearer auth.

The gateway is a stdio sidecar by default. Setting ``transport=http`` exposes the
MCP Streamable HTTP endpoint so remote and multi-client callers (web agents, a
shared deployment) can reach it. This module assembles the ASGI app; the security
posture (DNS-rebinding allowlist, oauth wiring, startup checks) lives in
``http_security`` and is applied to the FastMCP instance in ``build_server``.

Two things are owned here:

1. ``/health`` — an unauthenticated liveness probe (peer precedent: ClickHouse's
   ``/health``). It reports only that the process is up; it deliberately does NOT
   call the cluster (so a CC outage does not flap the gateway's liveness) and does
   NOT leak the version (fingerprinting). Readiness against the CC is answerable
   through ``get_cluster_status``.

2. Bearer authentication (``auth_mode='bearer'``) — a shared static token checked
   in constant time on every request except ``/health``. The richer
   ``auth_mode='oauth'`` path is wired into FastMCP itself (resource-server JWT
   validation) and needs no middleware here. ``auth_mode='none'`` serves open and
   is allowed only on a loopback bind (enforced in ``http_security``).

Authentication here is a boundary, not authorization: a valid credential grants
the full read-only surface. The read-only guarantee is enforced independently at
egress (``readonly=true`` on every CC query).
"""

from __future__ import annotations

import hmac
import logging
from typing import cast

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import HEALTH_PATH, Settings

logger = logging.getLogger(__name__)

_BEARER_SCHEME = "bearer"


async def health(_request: Request) -> JSONResponse:
    """Liveness probe: the process is up and serving. No cluster call, no version."""
    return JSONResponse({"status": "ok"})


def is_authorized(authorization_header: str | None, api_key: str) -> bool:
    """Return True iff the header carries a valid ``Bearer <api_key>`` credential.

    The token comparison is constant-time to avoid leaking it through timing.
    """
    if not authorization_header:
        return False
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != _BEARER_SCHEME or not token:
        return False
    return hmac.compare_digest(token, api_key)


class BearerAuthMiddleware:
    """ASGI middleware enforcing a static bearer token on every non-exempt path."""

    def __init__(self, app: ASGIApp, *, api_key: str, exempt_paths: frozenset[str]) -> None:
        self.app = app
        self.api_key = api_key
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Non-HTTP scopes (lifespan) and exempt paths bypass the credential check.
        if scope["type"] != "http" or scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        if not is_authorized(_authorization_header(scope), self.api_key):
            response = JSONResponse(
                {"error": "unauthorized", "message": "Missing or invalid bearer token."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _authorization_header(scope: Scope) -> str | None:
    """Extract the Authorization header value from a raw ASGI scope."""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            header: str = value.decode("latin-1")
            return header
    return None


def build_http_app(mcp: FastMCP, settings: Settings) -> Starlette:
    """Build the Streamable HTTP ASGI app: MCP endpoint, ``/health``, and auth.

    The MCP path/host/port and any oauth resource-server auth are already seeded on
    the FastMCP instance by ``build_server``. Here we append the health route and,
    for bearer mode, wrap everything except ``/health`` with the token check.
    """
    app = mcp.streamable_http_app()
    app.router.routes.append(Route(HEALTH_PATH, health, methods=["GET"]))

    if settings.auth_mode == "bearer":
        # api_key presence/length is guaranteed by validate_http_security.
        app.add_middleware(
            BearerAuthMiddleware,
            api_key=cast(str, settings.api_key),
            exempt_paths=frozenset({HEALTH_PATH}),
        )
    elif settings.auth_mode == "none":
        logger.warning(
            "HTTP transport is serving WITHOUT authentication (auth_mode='none'). "
            "This is allowed on a loopback bind only; use 'bearer' or 'oauth' otherwise."
        )
    return app
