"""Unit tests for the TTL-bounded submission audit log."""

from __future__ import annotations

import pytest

from asterixdb_mcp.audit_log import AuditEntry, AuditLog


class FakeClock:
    """A manually advanced clock for deterministic TTL tests."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _entry(ccid: str, at: float, *, handle: str | None = None) -> AuditEntry:
    return AuditEntry(
        client_context_id=ccid,
        session="sess",
        statement="SELECT 1;",
        submitted_at=at,
        handle=handle,
    )


def test_ttl_must_be_positive() -> None:
    with pytest.raises(ValueError, match="ttl_s must be positive"):
        AuditLog(0)


def test_record_and_get_round_trip() -> None:
    clock = FakeClock()
    log = AuditLog(ttl_s=100, clock=clock)
    log.record(_entry("a::b::c", clock()))

    fetched = log.get("a::b::c")
    assert fetched is not None
    assert fetched.statement == "SELECT 1;"


def test_get_unknown_returns_none() -> None:
    log = AuditLog(ttl_s=100)
    assert log.get("missing") is None


def test_entry_expires_after_ttl() -> None:
    clock = FakeClock()
    log = AuditLog(ttl_s=100, clock=clock)
    log.record(_entry("a::b::c", clock()))

    clock.advance(100)  # exactly at TTL boundary counts as expired
    assert log.get("a::b::c") is None


def test_entry_live_just_before_ttl() -> None:
    clock = FakeClock()
    log = AuditLog(ttl_s=100, clock=clock)
    log.record(_entry("a::b::c", clock()))

    clock.advance(99)
    assert log.get("a::b::c") is not None


def test_with_handle_is_immutable_update() -> None:
    entry = _entry("a::b::c", 1000.0)
    updated = entry.with_handle("/query/service/status/x")

    assert entry.handle is None
    assert updated.handle == "/query/service/status/x"
    assert updated.client_context_id == entry.client_context_id


def test_record_replaces_existing_entry() -> None:
    clock = FakeClock()
    log = AuditLog(ttl_s=100, clock=clock)
    log.record(_entry("a::b::c", clock()))
    log.record(_entry("a::b::c", clock(), handle="h"))

    fetched = log.get("a::b::c")
    assert fetched is not None
    assert fetched.handle == "h"


def test_forget_removes_entry() -> None:
    log = AuditLog(ttl_s=100)
    log.record(_entry("a::b::c", log.now()))
    log.forget("a::b::c")
    assert log.get("a::b::c") is None


def test_forget_unknown_is_noop() -> None:
    log = AuditLog(ttl_s=100)
    log.forget("nope")  # must not raise


def test_len_prunes_expired_entries() -> None:
    clock = FakeClock()
    log = AuditLog(ttl_s=100, clock=clock)
    log.record(_entry("a::b::c", clock()))
    log.record(_entry("d::e::f", clock()))
    assert len(log) == 2

    clock.advance(150)
    assert len(log) == 0


def test_record_prunes_expired_entries() -> None:
    clock = FakeClock()
    log = AuditLog(ttl_s=100, clock=clock)
    log.record(_entry("old", clock()))
    clock.advance(150)
    log.record(_entry("new", clock()))

    # The expired "old" entry is pruned on the next record.
    assert log.get("old") is None
    assert log.get("new") is not None


def test_to_public_shape() -> None:
    entry = _entry("a::b::c", 1000.0, handle="/query/service/status/x")
    public = entry.to_public()
    assert public == {
        "clientContextID": "a::b::c",
        "session": "sess",
        "statement": "SELECT 1;",
        "submittedAt": 1000.0,
        "handle": "/query/service/status/x",
        "resultHandle": None,
        "dataverse": None,
        "signature": None,
    }
