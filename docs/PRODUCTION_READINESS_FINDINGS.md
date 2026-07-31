# Production Readiness — Findings

**Date:** 2026-07-31
**Scope:** `src/asterixdb_mcp/` (13,261 lines Python)
**Method:** pattern-driven audit of production failure classes — unbounded scans,
unbounded growth, shared mutable state, blocking I/O in async paths, silent failure.
Not exhaustive: auth/crypto review, the in-engine Java `okf_catalog()` code, and
test-coverage gaps are not covered here.

**Nothing below is broken in the current single-user deployment.** Every finding is
a gap that opens under production conditions — concurrent clients, sustained query
volume, or long-lived processes.

---

## Severity C — Multi-tenant state confusion (HTTP transport)

The root cause is one line of structure: `build_server()` constructs four state
objects **once per process** (`server.py:606-610`), and the HTTP transport serves
**all clients from one process**. Three of those classes document themselves as
"session-scoped". In HTTP mode "session" silently means "process lifetime".

In stdio mode this is correct — one process per client. The gateway is deployed on
HTTP (`:19200`), so this is the mode that matters.

### C1 — Only the first client ever receives the start-of-session briefing
`server.py:609`, `tools/briefing.py:40`

`BriefingState` is a one-shot flag. Process-wide, so the first client to connect
spends it and **every subsequent client gets no briefing until the gateway
restarts.** Silent — no error, no log.

### C2 — The second client onward silently receives no memory notes
`server.py:608`, `tools/memory_notes.py:255`

`RecallState._delivered` records subjects whose notes were already delivered "this
session", to avoid burning context on repeats. Process-wide, so client A querying
`real_estate.homes` marks that subject delivered; client B querying the same dataset
is told notes were already delivered and **gets none**.

This defeats the memory layer's entire value proposition for every client except the
first, in the deployment mode actually shipped.

### C3 — Fabricated "fix" notes written across client boundaries
`server.py:607`, `tools/memory_capture.py:62-80`

`CaptureState._failures` is keyed **by subject only** — no session, no client. The
capture rule is "a failure then a later success on the same subject means the second
statement is the fix for the first."

Process-wide, with concurrent clients, that pairs *client A's failure* with *client
B's unrelated success* and persists a note asserting B's query fixes A's error. Wrong
knowledge, written automatically without model involvement, then served to everyone.

This is the most serious finding: it writes durable false facts rather than merely
withholding true ones. It is also a cross-tenant information path — one client's
statement text lands in a note surfaced to another.

### C4 — Distillation's "distinct sessions" counts gateway restarts
`config.py:310`, `distill.py` (`MIN_SESSIONS_DEFAULT = 2`)

`agent_session_id` gets a per-**process** random suffix. Distillation promotes a
statement to a proven note after success in ≥2 *distinct sessions*. Under HTTP, all
clients share one id, so the criterion measures **restart count, not independent
confirmation**. Corroboration is weaker than the design intends.

**Fix direction for C1–C4:** key all four states by MCP session id (available on
`Context` in SDK 2.x) rather than per process, with eviction on session end. C3 also
needs its `_failures` key widened to `(session, subject)` independent of the rest.

---

## Severity H — Unbounded growth and unbounded scans

### H1 — Distillation full-scans the entire episodic log into process memory
`distill.py:46`

```python
EVENTS_QUERY = f"SELECT VALUE e FROM {SESSION_EVENT_DATASET} e;"
```

No `LIMIT`, no time window. Every event ever recorded is pulled into a Python list,
then `dedupe_events()` and grouping run in process. This fires on every gateway
startup and, in HTTP mode, on `ASTERIXDB_MCP_DISTILL_INTERVAL_S`.

At 10⁶ queries/day this OOMs the gateway within days — well before storage matters.

**Fix:** push `GROUP BY` cluster-side (the design's own thesis: "consolidation is a
SQL++ expression, not pipeline code"), and window by `ts > $since`.

### H2 — `AgentMemory.SessionEvent` has no retention
No `DELETE`, TTL, or prune anywhere. `decay.py` only touches `AgentMemory.Memory`
rows with `type = "Note"` (`decay.py:34,41`) and never looks at the event log. It
grows without bound for the life of the deployment.

**Fix:** roll up to a counter per `(subject, statement_hash, outcome)`. Cardinality
then bounds to *distinct query shapes* — thousands, not millions — and distillation
only ever wanted aggregates. This is the structural fix; retention becomes moot.

### H3 — Event statements are stored untrimmed
`tools/memory_capture.py:163` — `"statement": statement`, raw.

`_trim()` (300 chars) is applied to notes, never to events. Row size is unbounded.
Rough estimate at ~500 B average: ~500 MB/day at 10⁶ queries/day, before LSM and
replication overhead.

### H4 — The offline JSONL buffer has no size cap — **fixed**
`tools/memory_capture.py`

Every event appended to a JSONL file with no cap, no rotation, no age limit, so a
full disk took the gateway down for reasons unrelated to memory.

Worse than first described: this was not only an outage path. The buffer is the
fallback whenever the cluster does not take an event, and with
`memory_write_enabled=false` — a supported configuration — *every* query outcome
took it, no outage required. In a multi-tenant gateway that makes it a denial of
service any agent can trigger by provoking query outcomes, against every tenant at
once. That is why it could not stay on the independent track.

Fixed with a byte budget (`session_log_max_bytes`, default 32 MiB) measured across
the whole buffer directory rather than one file: each session id gets its own file
and a crashed session's file is not reclaimed by its successor, so a per-file cap
would let N restarts cost N budgets. Over budget, events are dropped and the
condition is logged once per spell — the flood that fills the buffer must not
become a flood in the log.

### H6 — `datasets_from_sources()` fails open (latent; becomes a bypass when Phase 4 lands)
`plan_parser.py:139-140`

```python
elif default_dataverse:
    pair = (default_dataverse, parts[0])
# ...otherwise skipped
```

A single-segment plan data-source with no default dataverse is **silently dropped**
from the returned set.

Correct for its current caller: `plan_guard.check_columnar_scan()` emits an advisory,
and a missed advisory is harmless. **Wrong for an authorization check** — a source that
cannot be qualified simply disappears from the set tested against a per-principal
allowlist, so the statement is permitted rather than denied.

Not exploitable today: nothing authorizes on this function. It becomes a security bug
the moment Phase 4 uses it.

**Fix:** a strict variant that returns unresolved sources instead of discarding them, so
the allowlist check can deny on them. Advisories may fail open; authorization must fail
closed. One function cannot serve both policies.

### H5 — `RecallState._rows` never evicts
`tools/memory_notes.py:273`

Delivered note rows accumulate in a dict for the process lifetime with no bound. In
stdio (short-lived) this is fine; in a long-running HTTP gateway it is a slow leak,
compounded by C2 making the process the session.

---

## Severity M

### M1 — Synchronous file I/O inside async request paths
`tools/memory_capture.py:240` (`read_text`), `:253` (`unlink`), `:264` (`open`/write)

These run inside `await`ed coroutines on the request path and block the event loop.
Individually microseconds; under concurrent load with a large buffer, a stall.

### M2 — Buffer replay runs inline on a user's query
`tools/memory_capture.py:218-255`

`_flush_buffered_events` is called from `_record_session_event`, which is awaited
during `capture_query_outcome` — i.e. in the query path. It replays **every**
`*.jsonl` file in the directory, one INSERT per line, sequentially. After an outage
the first user query afterwards absorbs the entire backlog.

### M3 — `decay.py` scans all current notes with no limit
`decay.py:34` — grows with the memory store. Lower risk than H1 (the store is small
by design) but the same shape, and it fires on every startup.

---

## Suggested order

Ordered by production risk per unit of work, not by severity label.

| # | Finding | Why first |
|---|---|---|
| 1 | **H1** — window + cluster-side GROUP BY | Smallest diff, removes the OOM. One file. |
| 2 | **C3** — session-key `CaptureState` | Only finding that writes false data. |
| 3 | **C1, C2** — session-key briefing/recall | Same mechanism as C3; restores memory for all but the first client. |
| 4 | **H2, H3** — statement-hash rollup + trim | Structural; makes retention unnecessary. |
| 5 | **H4, M2** — buffer cap + move replay off the request path | Same file, one change. |
| 6 | **C4** | Falls out of the session-id work in 2–3. |
| 7 | **H5, M1, M3** | Cleanup once the shape is settled. |

Items 2, 3 and 6 are one refactor: thread the MCP session id through to the four
state objects. Doing them together is less work than doing them separately.

---

## Not yet examined

- `auth.py` / `http_security.py` / `permits.py` — auth and rate limiting
- `egress.py`, `statement_guard.py`, `plan_guard.py` — the guard layer
- In-engine Java: `OKFCatalogRewriter` and the `okf_catalog()` datasource path
- Test coverage of the concurrent-client paths above (the C-class findings suggest
  no test exercises two simultaneous clients)
