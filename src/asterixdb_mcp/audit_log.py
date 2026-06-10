"""Session-scoped, TTL-bounded submission audit log.

When a query is submitted (sync or async) the gateway records a small metadata
entry keyed by its namespaced ``clientContextID``: the statement, owning session,
submission time, and the CC handle once known. ``cancel_query`` and
``fetch_query_result`` look up that entry to recover the handle and to enforce
that a caller only touches its own submissions.

The log is in-memory and TTL-bounded: an entry older than the configured TTL is
treated as absent and dropped lazily on the next access. The CC remains the
authority on whether a query is actually still running; this log only remembers
enough to route a follow-up call and never holds CC resources open.

Entries are immutable. Attaching a handle produces a new entry rather than
mutating the stored one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class AuditEntry:
    """Immutable metadata for one submitted query."""

    client_context_id: str
    session: str
    statement: str
    submitted_at: float
    handle: str | None = None
    result_handle: str | None = None
    dataverse: str | None = None
    signature: Any = None
    # Non-fatal columnar full-scan advisory payload captured at submission, so
    # fetch_query_result can minimize output and re-surface the flag.
    advisory: dict[str, Any] | None = None

    def with_handle(self, handle: str) -> AuditEntry:
        """Return a copy carrying the CC status handle (immutable update)."""
        return replace(self, handle=handle)

    def with_result_handle(self, result_handle: str) -> AuditEntry:
        """Return a copy carrying the CC result handle (immutable update)."""
        return replace(self, result_handle=result_handle)

    def to_public(self) -> dict[str, Any]:
        """Render the LLM-safe view of this entry (no internal-only fields)."""
        return {
            "clientContextID": self.client_context_id,
            "session": self.session,
            "statement": self.statement,
            "submittedAt": self.submitted_at,
            "handle": self.handle,
            "resultHandle": self.result_handle,
            "dataverse": self.dataverse,
            "signature": self.signature,
            "advisories": [self.advisory] if self.advisory else [],
        }


class AuditLog:
    """An in-memory, TTL-bounded map of ``clientContextID`` to AuditEntry."""

    def __init__(self, ttl_s: float, *, clock: Callable[[], float] | None = None) -> None:
        if ttl_s <= 0:
            raise ValueError(f"audit log ttl_s must be positive, got {ttl_s}")
        self._ttl_s = ttl_s
        self._clock = clock or time.time
        self._entries: dict[str, AuditEntry] = {}

    def now(self) -> float:
        """Current time from the injected clock (used to stamp submissions)."""
        return self._clock()

    def record(self, entry: AuditEntry) -> None:
        """Store (or replace) the entry for its clientContextID."""
        self._prune()
        self._entries[entry.client_context_id] = entry

    def get(self, client_context_id: str) -> AuditEntry | None:
        """Return the live entry, or None if it is absent or has expired."""
        entry = self._entries.get(client_context_id)
        if entry is None:
            return None
        if self._is_expired(entry):
            del self._entries[client_context_id]
            return None
        return entry

    def forget(self, client_context_id: str) -> None:
        """Drop an entry if present (e.g. after a confirmed cancel)."""
        self._entries.pop(client_context_id, None)

    def __len__(self) -> int:
        """Live (non-expired) entry count; prunes as a side effect."""
        self._prune()
        return len(self._entries)

    def _is_expired(self, entry: AuditEntry) -> bool:
        return self._clock() - entry.submitted_at >= self._ttl_s

    def _prune(self) -> None:
        expired = [k for k, v in self._entries.items() if self._is_expired(v)]
        for key in expired:
            del self._entries[key]
