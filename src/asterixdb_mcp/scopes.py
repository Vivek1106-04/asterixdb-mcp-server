"""Per-connection state, keyed by who is calling and from where.

Three of the gateway's state objects describe themselves as session-scoped:
``BriefingState`` spends a one-shot flag, ``RecallState`` records which subjects
have already had their notes delivered, and ``CaptureState`` holds failed
statements waiting for a working form. Under stdio that description was true —
one process per client. Under HTTP one process serves every client, so a single
shared instance of each meant "session" silently became "process lifetime", and
all three broke in different directions:

* only the first client ever to connect received a briefing;
* the second client onward was told notes were already delivered, and got none;
* one client's failure could be paired with another client's unrelated success,
  writing a durable note that asserted the second query fixed the first.

This module binds one bundle of those objects to each connection. Bundles are
held weakly against the SDK's session object, so a finished connection releases
its state with no eviction machinery, no TTL, and no background sweep.

A call with no session object at all falls back to keying by tenant. That map is
held strongly, but it is bounded by the number of distinct tenants rather than by
traffic, and it is only reachable on a path that has no connection to hang state
on in the first place.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

from .identity import Identity, resolve_identity
from .tools.briefing import BriefingState
from .tools.memory_capture import CaptureState
from .tools.memory_notes import RecallState


@dataclass(frozen=True)
class SessionScope:
    """One connection's private state, plus the identity that owns it."""

    identity: Identity
    capture: CaptureState = field(default_factory=CaptureState)
    recall: RecallState = field(default_factory=RecallState)
    briefing: BriefingState = field(default_factory=BriefingState)


class ScopeRegistry:
    """Hands each connection its own ``SessionScope``."""

    def __init__(self) -> None:
        self._by_session: WeakKeyDictionary[Any, SessionScope] = WeakKeyDictionary()
        self._connectionless: dict[str, SessionScope] = {}

    def for_call(self, ctx: Any, settings: Any) -> SessionScope:
        """The scope owning this call, created on first use."""
        who = resolve_identity(ctx, settings)
        session = _session_object(ctx)

        if session is None:
            scope = self._connectionless.get(who.principal)
            if scope is None:
                scope = SessionScope(identity=who)
                self._connectionless[who.principal] = scope
            return scope

        scope = self._by_session.get(session)
        # A principal change on a live connection should be impossible: the SDK
        # binds a session to the credential that created it. Rebuilding rather
        # than reusing means that if the assumption ever breaks, one tenant
        # inherits nothing from another.
        if scope is None or scope.identity.principal != who.principal:
            scope = SessionScope(identity=who)
            self._by_session[session] = scope
        return scope

    def live_count(self) -> int:
        """How many scopes are currently held. Diagnostics and tests."""
        return len(self._by_session) + len(self._connectionless)


def _session_object(ctx: Any) -> Any:
    """The SDK session behind this call, or None when there is none to hold."""
    try:
        session = getattr(ctx, "session", None)
    except Exception:
        # ``Context.session`` is a property that can raise outside a request.
        return None
    if session is None:
        return None
    # A session we cannot hold weakly, or cannot hash, cannot key the registry;
    # treating it as connectionless is correct rather than merely safe.
    try:
        weakref.ref(session)
        hash(session)
    except TypeError:
        return None
    return session
