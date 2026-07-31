"""Who is asking, and on which connection.

The gateway serves every HTTP client from one process, so "who" has to be read
per call rather than per process. Two keys come out of that, and they are not
interchangeable:

* **principal** — the tenant. Derived only from a *verified* access token, so it
  is as trustworthy as the authorization server that issued it. It spans
  connections: the same user opening a second client is the same tenant.
* **session** — the connection. One MCP conversation. It is what per-session
  state (briefing, recall, capture) must be keyed by.

Keying on either one alone is wrong. Principal alone re-breaks per-conversation
state for a single user's second client; session alone leaves tenants sharing
durable memory.

The session key is deliberately **not** read from the ``Mcp-Session-Id`` header.
That header is client-supplied — the SDK's own docstring warns never to treat a
header as an identity assertion — and a spoofed value would graft one caller
onto another's state. Instead the key is bound to the SDK's per-connection
session *object*, which the transport owns and a client cannot forge. Holding
those bindings weakly means a finished connection releases its key without any
eviction machinery of our own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from mcp.server.auth.middleware.auth_context import get_access_token

# A client_id is chosen by whoever registered with the authorization server, and
# it keys dicts here and lands in memory rows later, so it is bounded on the way in.
MAX_PRINCIPAL_LEN = 80

# No verified token: stdio, or HTTP with auth_mode="none". A single local tenant.
LOCAL_PRINCIPAL = "local"

# auth_mode="bearer" is one shared secret, so it can prove a caller is allowed in
# but can never say *which* caller. Every bearer client is therefore one tenant.
# Real multi-tenancy needs auth_mode="oauth".
SHARED_BEARER_PRINCIPAL = "shared-bearer"

# A call with no session object at all. Rare, and it must not silently share a
# key with a real connection, so it gets its own reserved name.
ORPHAN_SESSION = "orphan"

_session_keys: WeakKeyDictionary[Any, str] = WeakKeyDictionary()


@dataclass(frozen=True)
class Identity:
    """The tenant behind a call and the connection it arrived on."""

    principal: str
    session: str


def resolve_principal(settings: Any) -> str:
    """The tenant for the current call, from the verified token when there is one.

    Falls back to a mode-derived constant rather than raising: identity is read
    on every call, and a request that cannot be attributed should be attributed
    conservatively, not rejected here. Authorization is a separate decision that
    happens downstream with this value in hand.
    """
    try:
        token = get_access_token()
    except Exception:
        # Outside a request context the contextvar lookup can fail; that is
        # indistinguishable from an unauthenticated call, and both mean "no tenant".
        token = None

    if token is not None:
        client_id = str(getattr(token, "client_id", "") or "").strip()
        if client_id:
            return client_id[:MAX_PRINCIPAL_LEN]

    # An empty client_id falls through here on purpose. Keying state under ""
    # would merge unrelated callers into one tenant, which is worse than
    # treating the call as unattributed.
    if getattr(settings, "auth_mode", "none") == "bearer":
        return SHARED_BEARER_PRINCIPAL
    return LOCAL_PRINCIPAL


def session_key(ctx: Any) -> str:
    """A stable, unforgeable key for the connection this call arrived on.

    Bound to the SDK's session object rather than to any header, and held weakly
    so the key disappears with the connection.
    """
    try:
        session = getattr(ctx, "session", None)
    except Exception:
        # ``Context.session`` is a property that can raise outside a request.
        return ORPHAN_SESSION

    if session is None:
        return ORPHAN_SESSION

    try:
        key = _session_keys.get(session)
        if key is None:
            key = uuid.uuid4().hex
            _session_keys[session] = key
        return key
    except TypeError:
        # Not weak-referenceable or not hashable. Better an explicit orphan key
        # than a crash on every call.
        return ORPHAN_SESSION


def resolve_identity(ctx: Any, settings: Any) -> Identity:
    """The full ``(principal, session)`` key for the current call."""
    return Identity(principal=resolve_principal(settings), session=session_key(ctx))


def tracked_session_count() -> int:
    """How many live connections currently hold a key. Diagnostics and tests."""
    return len(_session_keys)
