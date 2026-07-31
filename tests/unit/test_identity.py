"""Unit tests for caller identity: the tenant, and the connection it arrived on."""

from __future__ import annotations

import gc
from types import SimpleNamespace

import pytest

from asterixdb_mcp import identity
from asterixdb_mcp.identity import (
    LOCAL_PRINCIPAL,
    SHARED_BEARER_PRINCIPAL,
    Identity,
    resolve_identity,
    resolve_principal,
    session_key,
)


class _Session:
    """Weak-referenceable stand-in for the SDK's per-connection ServerSession."""


def _ctx(session: object | None = None) -> SimpleNamespace:
    """A stand-in for the SDK Context carrying only what identity reads."""
    return SimpleNamespace(session=_Session() if session is None else session)


def _settings(auth_mode: str = "none") -> SimpleNamespace:
    return SimpleNamespace(auth_mode=auth_mode)


def test_principal_is_local_when_no_token_is_present(monkeypatch) -> None:
    monkeypatch.setattr(identity, "get_access_token", lambda: None)

    assert resolve_principal(_settings("none")) == LOCAL_PRINCIPAL


def test_principal_is_the_verified_client_id_under_oauth(monkeypatch) -> None:
    monkeypatch.setattr(identity, "get_access_token", lambda: SimpleNamespace(client_id="tenant-a"))

    assert resolve_principal(_settings("oauth")) == "tenant-a"


def test_bearer_mode_collapses_every_caller_onto_one_principal(monkeypatch) -> None:
    # Bearer is a single shared secret, so it cannot name a tenant. The SDK auth
    # context stays empty because bearer is checked by our own middleware, not
    # the SDK's resource-server machinery.
    monkeypatch.setattr(identity, "get_access_token", lambda: None)

    assert resolve_principal(_settings("bearer")) == SHARED_BEARER_PRINCIPAL


def test_a_token_without_a_client_id_does_not_become_an_anonymous_tenant(monkeypatch) -> None:
    # Keying state under "" would merge unrelated callers into one tenant.
    monkeypatch.setattr(identity, "get_access_token", lambda: SimpleNamespace(client_id=""))

    assert resolve_principal(_settings("oauth")) == LOCAL_PRINCIPAL


def test_principal_survives_an_auth_context_lookup_failure(monkeypatch) -> None:
    # Identity is read on every call, so it degrades rather than failing the call.
    def boom() -> None:
        raise LookupError("no auth context")

    monkeypatch.setattr(identity, "get_access_token", boom)

    assert resolve_principal(_settings("oauth")) == LOCAL_PRINCIPAL


def test_principal_is_length_capped(monkeypatch) -> None:
    # client_id is attacker-influenced, keys dicts, and later lands in memory rows.
    monkeypatch.setattr(identity, "get_access_token", lambda: SimpleNamespace(client_id="x" * 500))

    assert len(resolve_principal(_settings("oauth"))) == identity.MAX_PRINCIPAL_LEN


def test_session_key_is_stable_for_the_same_connection() -> None:
    ctx = _ctx()

    assert session_key(ctx) == session_key(ctx)


def test_session_key_differs_across_connections() -> None:
    assert session_key(_ctx()) != session_key(_ctx())


def test_session_key_is_not_read_from_client_supplied_headers() -> None:
    # A client that spoofs a session header must not graft itself onto another
    # connection's state.
    victim_key = session_key(_ctx())
    attacker = _ctx()
    attacker.headers = {"mcp-session-id": victim_key}

    assert session_key(attacker) != victim_key


def test_session_key_falls_back_when_no_session_object_exists() -> None:
    assert session_key(SimpleNamespace(session=None)) == identity.ORPHAN_SESSION


def test_session_key_falls_back_when_reading_the_session_raises() -> None:
    # Context.session is a property that raises outside an active request.

    class _Ctx:
        @property
        def session(self) -> object:
            raise RuntimeError("outside a request context")

    assert session_key(_Ctx()) == identity.ORPHAN_SESSION


def test_session_key_falls_back_when_the_session_cannot_be_weakly_held() -> None:
    # A bare object() is hashable but not weak-referenceable; an orphan key beats
    # raising on every call.
    assert session_key(SimpleNamespace(session=object())) == identity.ORPHAN_SESSION


def test_session_keys_are_released_when_the_connection_is_collected() -> None:
    # The registry must not pin dead sessions, or it leaks in a long-running gateway.
    session = _Session()
    session_key(_ctx(session))
    before = identity.tracked_session_count()

    del session
    gc.collect()

    assert identity.tracked_session_count() < before


def test_resolve_identity_combines_principal_and_session(monkeypatch) -> None:
    monkeypatch.setattr(identity, "get_access_token", lambda: SimpleNamespace(client_id="tenant-a"))
    ctx = _ctx()

    who = resolve_identity(ctx, _settings("oauth"))

    assert who.principal == "tenant-a"
    assert who.session == session_key(ctx)


def test_identity_is_hashable_so_it_can_key_state() -> None:
    who = Identity(principal="a", session="s")

    assert {who: 1}[Identity(principal="a", session="s")] == 1


def test_same_user_on_two_connections_is_one_tenant_but_two_sessions(monkeypatch) -> None:
    # This is why the key is a pair: keying on principal alone would re-break
    # per-conversation state for one user's second client.
    monkeypatch.setattr(identity, "get_access_token", lambda: SimpleNamespace(client_id="tenant-a"))

    first = resolve_identity(_ctx(), _settings("oauth"))
    second = resolve_identity(_ctx(), _settings("oauth"))

    assert first.principal == second.principal
    assert first.session != second.session


def test_identity_is_immutable() -> None:
    who = Identity(principal="a", session="s")

    with pytest.raises(AttributeError):
        who.principal = "b"  # type: ignore[misc]
