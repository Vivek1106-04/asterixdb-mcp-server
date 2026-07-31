"""Unit tests for per-connection state scoping.

The regression tests here are the C1/C2/C3 findings: three state objects that
documented themselves as session-scoped but were built once per process, so in
HTTP mode every client shared them.
"""

from __future__ import annotations

import gc
from types import SimpleNamespace

from asterixdb_mcp import identity
from asterixdb_mcp.scopes import ScopeRegistry, SessionScope


class _Session:
    """Weak-referenceable stand-in for the SDK's per-connection ServerSession."""


def _ctx(session: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(session=_Session() if session is None else session)


def _settings(auth_mode: str = "none") -> SimpleNamespace:
    return SimpleNamespace(auth_mode=auth_mode)


def _as_tenant(monkeypatch, client_id: str) -> None:
    monkeypatch.setattr(identity, "get_access_token", lambda: SimpleNamespace(client_id=client_id))


def test_the_same_connection_gets_the_same_scope() -> None:
    registry = ScopeRegistry()
    ctx = _ctx()

    assert registry.for_call(ctx, _settings()) is registry.for_call(ctx, _settings())


def test_different_connections_get_different_scopes() -> None:
    registry = ScopeRegistry()

    first = registry.for_call(_ctx(), _settings())
    second = registry.for_call(_ctx(), _settings())

    assert first is not second


def test_a_scope_carries_the_identity_that_owns_it(monkeypatch) -> None:
    _as_tenant(monkeypatch, "tenant-a")
    registry = ScopeRegistry()

    scope = registry.for_call(_ctx(), _settings("oauth"))

    assert scope.identity.principal == "tenant-a"


def test_a_scope_is_rebuilt_if_the_principal_on_a_connection_changes(monkeypatch) -> None:
    # The SDK binds a session to the credential that created it, so this should
    # not happen. If that assumption ever breaks, state must not bleed between
    # tenants — a changed principal gets a fresh scope rather than the old one.
    registry = ScopeRegistry()
    ctx = _ctx()
    _as_tenant(monkeypatch, "tenant-a")
    first = registry.for_call(ctx, _settings("oauth"))

    _as_tenant(monkeypatch, "tenant-b")
    second = registry.for_call(ctx, _settings("oauth"))

    assert first is not second
    assert second.identity.principal == "tenant-b"


def test_scopes_are_released_when_the_connection_is_collected() -> None:
    registry = ScopeRegistry()
    session = _Session()
    registry.for_call(_ctx(session), _settings())
    before = registry.live_count()

    del session
    gc.collect()

    assert registry.live_count() < before


def test_a_connectionless_call_is_scoped_by_tenant(monkeypatch) -> None:
    # No session object to hang a scope on, so the tenant is the only key left.
    registry = ScopeRegistry()
    _as_tenant(monkeypatch, "tenant-a")
    ctx = SimpleNamespace(session=None)

    first = registry.for_call(ctx, _settings("oauth"))
    second = registry.for_call(ctx, _settings("oauth"))

    assert first is second


def test_connectionless_calls_from_different_tenants_stay_apart(monkeypatch) -> None:
    registry = ScopeRegistry()
    ctx = SimpleNamespace(session=None)
    _as_tenant(monkeypatch, "tenant-a")
    first = registry.for_call(ctx, _settings("oauth"))

    _as_tenant(monkeypatch, "tenant-b")
    second = registry.for_call(ctx, _settings("oauth"))

    assert first is not second


def test_a_call_whose_session_lookup_raises_is_treated_as_connectionless(monkeypatch) -> None:
    # Context.session is a property that raises outside an active request.
    _as_tenant(monkeypatch, "tenant-a")
    registry = ScopeRegistry()

    class _Ctx:
        @property
        def session(self) -> object:
            raise RuntimeError("outside a request context")

    assert registry.for_call(_Ctx(), _settings("oauth")) is registry.for_call(
        _Ctx(), _settings("oauth")
    )


def test_a_session_that_cannot_be_held_weakly_is_treated_as_connectionless(monkeypatch) -> None:
    # A bare object() is hashable but not weak-referenceable, so it cannot key
    # the registry without pinning state for the life of the process.
    _as_tenant(monkeypatch, "tenant-a")
    registry = ScopeRegistry()

    scope = registry.for_call(SimpleNamespace(session=object()), _settings("oauth"))

    assert scope.identity.principal == "tenant-a"
    assert registry.live_count() == 1


def test_a_fresh_scope_has_its_own_state_objects() -> None:
    registry = ScopeRegistry()

    first = registry.for_call(_ctx(), _settings())
    second = registry.for_call(_ctx(), _settings())

    assert first.capture is not second.capture
    assert first.recall is not second.recall
    assert first.briefing is not second.briefing


def test_one_clients_failure_is_never_paired_with_anothers_success() -> None:
    # C3: CaptureState keyed failures by subject alone, process-wide. Two
    # concurrent clients touching one dataset produced a note asserting that
    # B's unrelated query fixed A's error, written durably and served to both.
    registry = ScopeRegistry()
    a = registry.for_call(_ctx(), _settings())
    b = registry.for_call(_ctx(), _settings())

    a.capture.record("SELECT bad FROM dv.ds", "TYPE_ERROR")
    leaked = b.capture.record("SELECT good FROM dv.ds", None)

    assert leaked == []


def test_a_clients_own_failure_and_fix_are_still_paired() -> None:
    # The C3 fix must not disable capture; within one connection it still works.
    registry = ScopeRegistry()
    scope = registry.for_call(_ctx(), _settings())

    scope.capture.record("SELECT bad FROM dv.ds", "TYPE_ERROR")
    captured = scope.capture.record("SELECT good FROM dv.ds", None)

    assert [subject for subject, _ in captured] == ["dv.ds"]


def test_one_client_consuming_notes_does_not_starve_another() -> None:
    # C2: RecallState._delivered was process-wide, so the second client onward
    # was told notes had already been delivered and silently received none.
    registry = ScopeRegistry()
    a = registry.for_call(_ctx(), _settings())
    b = registry.for_call(_ctx(), _settings())

    a.recall.mark(["dv.ds"])

    assert b.recall.fresh(["dv.ds"]) == ["dv.ds"]


def test_every_connection_gets_its_own_briefing() -> None:
    # C1: BriefingState was a process-wide one-shot flag, so only the first
    # client ever to connect received a briefing.
    registry = ScopeRegistry()
    a = registry.for_call(_ctx(), _settings())
    b = registry.for_call(_ctx(), _settings())

    a.briefing.mark()

    assert a.briefing.pending() is False
    assert b.briefing.pending() is True


def test_scope_is_immutable() -> None:
    registry = ScopeRegistry()
    scope = registry.for_call(_ctx(), _settings())

    assert isinstance(scope, SessionScope)
    try:
        scope.capture = None  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("SessionScope should be frozen")
