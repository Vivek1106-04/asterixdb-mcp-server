# AsterixDB MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) gateway for
[Apache AsterixDB](https://asterixdb.apache.org/). It lets an LLM agent discover
datasets, inspect schemas (including ROW vs COLUMNAR storage), and run
**read-only** SQL++ queries against a live AsterixDB cluster.

## Architecture invariant

The gateway is a **standalone sidecar**. It never parses SQL++, never holds
Cluster Controller (CC) state, and never runs a mutation deny-list. The CC's
`readonly=true` parameter is the single authority on mutation rejection — the
gateway hardcodes it on every query. This keeps the database control plane
stateless with respect to LLM sessions.

```
LLM client  ──MCP (stdio | HTTP)──▶  AsterixDB MCP Gateway  ──HTTP──▶  AsterixDB CC
                                       (this repo)                    /query/service
                                                                      /admin/*
```

## Capabilities

**29 tools, 12 resources, 6 resource templates, 6 prompts.** Tools perform
actions; resources expose read-only context a client can attach to a session;
resource templates expose that context per dataverse/dataset via a URI pattern;
prompts are guided multi-step workflows.

Every tool advertises MCP behavioral annotations (`readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`) so a client can tell a
safe read from a state-changing call without parsing the description — the whole
surface is read-only except `cancel_query`, `memory_write`, and
`remember_preference`, and nothing is destructive. Prompt
and resource-template arguments support live `completion/complete`: typing a
`dataverse`, `dataset`, or grouping/metric field completes from the cluster's
real metadata, scoped by any argument already chosen.

Every tool also advertises an `outputSchema` describing its successful result
shape, so a client can anticipate the payload and chain calls (e.g. that
`submit_async_query` yields the `clientContextID` `fetch_query_result` consumes).
The schema characterizes successful results only; an error is flagged with
`isError` and carries the gateway error envelope, so advertisement never causes
a failed call to be rejected.

### Tools

| Group | Tool | Purpose |
|-------|------|---------|
| Query | `execute_query` | Synchronous read-only SQL++ with offset/limit windowing. |
| Query | `submit_async_query` | Submit a long-running query; returns a handle. |
| Query | `wait_on_async_query` | Long-poll an async handle to completion, reporting MCP progress each poll. |
| Query | `fetch_query_result` | Page through a completed async result set. |
| Query | `cancel_query` | Cancel an in-flight async query. |
| Analyze | `validate_syntax` | Compile-only syntax check, no execution. |
| Analyze | `explain_query` | Optimizer plan for a statement. |
| Analyze | `explain_physical_plan` | Physical Hyracks job: operator/connector DAG and parallelism. |
| Analyze | `check_index_usage` | Whether a query's predicates hit an index. |
| Analyze | `recommend_indexes` | CREATE INDEX advice from a workload via the cluster's native `ADVISE` advisor. |
| Analyze | `profile_query` | Run a query with profiling; per-operator runtime actuals (EXPLAIN ANALYZE). |
| Discover | `list_dataverses` | Enumerate dataverses on the cluster. |
| Discover | `list_datasets` | Paginated dataset discovery, optionally scoped to a dataverse. |
| Discover | `describe_dataverse` | Datasets, types, indexes, and functions in one dataverse. |
| Discover | `get_schema` | Single-dataset schema incl. `datasetFormatInfo` (ROW/COLUMNAR). |
| Discover | `sample_dataset` | A small bounded row sample from a dataset. |
| Discover | `search_metadata` | Cross-metadata search for datasets/types/indexes/functions. |
| Discover | `get_dataset_statistics` | Sampled row-count/size estimate and ANALYZE freshness for a dataset. |
| Memory | `memory_search` | OKF concept docs (schema, stats, proven queries) by subject key, full text, and link hop. |
| Memory | `memory_write` | Persist one agent-curated note bi-temporally (opt-in: `ASTERIXDB_MCP_MEMORY_WRITE_ENABLED`; scoped to the memory store). |
| Memory | `remember_preference` | Record a durable query-writing rule or stylistic preference (global or per-dataverse); preferences never decay and surface in the session briefing. |
| Functions | `list_functions` | Built-in / user-defined functions, filtered by language. |
| Functions | `get_function` | One function's signature, with near-name hints on a miss. |
| Cluster | `get_cluster_status` | Live cluster state and node roster. |
| Cluster | `get_node_details` | Per-node diagnostics for a validated node id. |
| Cluster | `list_running_queries` | In-flight cluster requests; the read side of the cancel lifecycle. |
| Health | `database_health_check` | Metadata scan for duplicate/redundant indexes and ROW-vs-COLUMNAR candidates. |
| Health | `get_query_history` | Recent session queries with outcome and classified error, for self-debugging. |
| Docs | `get_reference` | SQL++ reference docs by topic. |


The `memory_search` store is populated by `scripts/okf_refresh.py`, which walks
the engine's `okf_catalog()` built-in and reconciles the emitted OKF concept
docs into `AgentMemory.Memory` bi-temporally (changed facts are superseded, never
deleted). The gateway itself stays read-only; the refresh script is the write
path and talks to the CC query service directly. Requires a cluster whose
engine ships `okf_catalog()`. With `--ground` the refresh also executes each
doc's profiling queries (real row counts) and runs the native `ADVISE` advisor
over observed workload, folding results into the docs before reconcile.
With `--revalidate` it re-runs the stored `source_query` fingerprints of
learned memories and supersedes rows whose live results drifted (tool-wins).
It writes as the identity this deployment's own requests resolve to, so the
rows it materializes are the ones the gateway then reads; `--principal` points
it at another tenant's tier instead.
`scripts/okf_bundle.py` round-trips the store as a portable OKF bundle:
one markdown file per concept, per-dataverse `index.md`, and `log.md`
synthesized from the bi-temporal supersede chain.
`scripts/okf_consolidate.py` is the sleep-time pass: it deduplicates current
rows, decays each learned memory's `trust` by usage-modulated half-life, and
supersedes rows that fall below the trust floor (forgetting, with history
retained). `memory_search` accepts `link_depth` (1–2) to pull multi-hop
connected context along the concept link graph.

Recall is ranked and self-reinforcing: when a query touches datasets with many
notes, only the strongest attach (grounded knowledge and notes that keep
proving useful outrank unread assertions), and every delivery bumps the
delivered rows' usage counters — which feed both the ranking and the decay
pass, so useful notes stay and dead weight ages out. Ambient recall also
follows each hit's concept links one hop — a dataset's note pulls in learned
notes on its indexes and datatype — with the combined pool still capped and
ranked, so related context arrives without extra tool calls.

The first discovery call of a session (`list_dataverses`, `list_datasets`, or
`get_schema`) carries a one-time **session briefing**: the dataverse/dataset
inventory (with COLUMNAR counts), any active preferences, and the
how-to-query-here rules — so the model is oriented before its first query.
It is shown once per session, never attached to `execute_query` (whose text
mirrors its structured result), and degrades silently if the catalog or store
is unreachable. `remember_preference` feeds it: a recorded rule ("project
columns instead of SELECT *", "prefer dataverse X") is stored apart from
data-facts — never decayed, never evidence-scored, scoped `global` or
per-dataverse, and deduplicated on rewrite. Preference writes ride the same
`ASTERIXDB_MCP_MEMORY_WRITE_ENABLED` gate as `memory_write`.

`memory_write` is the one agent-facing write surface: it persists a curated
note bi-temporally — new subjects become standalone note concepts, notes on
catalog concepts land in the learned overlay and survive refreshes. A write
whose numbers contradict an existing note on the same subject (a different
value under the same nearby word, outside the stored range) still lands — truth
is append-only — but the response flags the possible conflict so the model
resolves it with `replaces`. It is off
by default (`ASTERIXDB_MCP_MEMORY_WRITE_ENABLED=true` enables it) and the CC
client refuses anything on that path except INSERT/UPSERT into the
`AgentMemory` store (the `Memory` concept dataset and the insert-only
`SessionEvent` episodic record); every other statement the gateway issues
stays `readonly=true`. `scripts/memory_eval.py` measures the memory layer on four
axes — recall (hit@k, MRR), forgetting (stale-recall rate), context
compression, and reuse of stored query patterns — from a JSONL case file, so
retrieval changes are tuned against numbers rather than asserted.

Capture is automatic as well as agent-curated. The gateway watches its own
query outcomes: when a statement against a dataset fails and a later statement
against the same dataset succeeds in the same session, the error->fix pair is
distilled into a learned note on that dataset's concept — no model involvement
(requires memory writes enabled). Failure means more than a raised error: a
query that runs but returns 0 rows WITH type-mismatch warnings is treated as a
silent semantic miss and captured the same way, and an async query that ends
in a terminal `timeout`/`failed`/`fatal` status feeds the same pipeline as a
synchronous error. Every query outcome is also
recorded as one episodic event — carrying the connected client's identity
(from the MCP handshake) and the query's performance metrics — into
`AgentMemory.SessionEvent` on the cluster when memory writes are enabled,
with the per-session JSONL file under
`ASTERIXDB_MCP_SESSION_LOG_DIR` acting as the offline buffer (buffered events
flush to the cluster on the next successful write). `scripts/memory_distill.py`
distills the cross-session signal from both sources: queries proven across
sessions become grounded notes (their statement is stored as `source_query`,
so revalidation keeps them honest), error classes that repeat without a
recorded success become caution notes, and statements that keep succeeding but
average above a wall-clock threshold become performance-caution notes (kept
unverified — a heuristic from past timings, not a proven fact). On the HTTP transport the gateway can
run that pass itself — set `ASTERIXDB_MCP_DISTILL_INTERVAL_S` (seconds, with
memory writes enabled) and consolidation happens on an interval with no
operator involvement.

With memory writes enabled, the gateway also runs a bounded **startup
maintenance pass** on every launch (any transport): it creates the AgentMemory
store if absent (only the exact canonical `IF NOT EXISTS` DDL is allowed
through the write guard, byte-for-byte), re-walks `okf_catalog()` so the
concept store tracks schema changes automatically (idempotent — an unchanged
catalog writes nothing; overlays always survive), runs a decay pass that
archives dead-weight notes (standalone, unverified, and never recalled within
30 days) bi-temporally so history keeps them, and runs one distill pass.
Concurrent gateway instances elect a single runner via an exclusive lock file
(0600, stale-broken after 10 minutes), the whole pass is capped at 60 seconds,
and every step degrades gracefully — a cluster without `okf_catalog()` simply
skips the walk. Disable with `ASTERIXDB_MCP_AUTO_MAINTENANCE_ENABLED=false`.

`ASTERIXDB_MCP_MEMORY_ENABLED=false` is the master switch for the whole memory
surface: it hides the memory tools (`memory_search`, `memory_write`,
`remember_preference`), skips the session briefing and ambient learned-note
recall, and disables episodic capture, startup maintenance, and the distill
loop — the gateway serves the plain catalog/query surface only. It overrides
`ASTERIXDB_MCP_MEMORY_WRITE_ENABLED` when off.

Writes are deduplicated beyond exact repeats: a note whose content tokens are
almost all present in an existing note on the same subject is rejected as a
near-duplicate paraphrase (the response quotes the stored note). Notes that
carry a new figure, a `source_query`, or a `replaces` correction always land.
End-user effect: install, point at a cluster, done — no scripts, no cron. `scripts/memory_migrate_labels.py` is a one-time
migration that labels overlay notes written before evidence labels existed as
`(unverified)`, so legacy claims stop reading as verified fact.

Delivered notes are checked against the rows they ride with, and against later
results in the same session — recall fires once per dataset, usually on the
sample taken before any query, so the aggregate that can disprove a stored
count arrives after the notes have already gone out. When a note's
figure disagrees with a fresh value under the same category, or the note types
a field the result returns as another type — or a sample carries a field under
a name the note does not know — the response names the
disagreement and asks for a correction, and the note is marked `possibly
stale` so a later session inherits the doubt. Detection only ever flags — the
note is still delivered, its text is never rewritten, and the gateway never
retires knowledge on its own inference.

### Resources

| URI | Purpose |
|-----|---------|
| `asterixdb://version` | AsterixDB + gateway version; liveness probe. |
| `asterixdb://cluster/status` | Live cluster state from `/admin/cluster`. |
| `asterixdb://cluster/diagnostics` | Aggregated per-node health diagnostics. |
| `asterixdb://config-parameters` | Effective gateway egress/timeout settings. |
| `asterixdb://dataverses` | Dataverse inventory. |
| `asterixdb://reference/sqlpp-syntax` | SQL++ syntax rules. |
| `asterixdb://reference/builtin-functions` | Built-in function catalog. |
| `asterixdb://reference/index-types` | Supported index types. |
| `asterixdb://reference/type-system` | SQL++ / ADM type system. |
| `asterixdb://reference/error-codes` | Gateway error taxonomy. |
| `asterixdb://reference/query-examples` | Worked SQL++ examples. |
| `asterixdb://reference/query-hints` | Inline SQL++ optimizer hints. |

### Resource templates

Parameterized URIs a client fills in to attach dataverse- or dataset-scoped
context without a tool call. The `{variables}` resolve against live `Metadata`,
so any dataverse or dataset added later works with no code change, and they
autocomplete through `completion/complete`.

| URI template | Purpose |
|--------------|---------|
| `asterixdb://schema/{dataverse}/{dataset}` | One dataset's declared schema incl. storage format. |
| `asterixdb://dataverse/{dataverse}` | Full schema of every dataset in a dataverse. |
| `asterixdb://sample/{dataverse}/{dataset}` | A small bounded sample of real documents. |
| `asterixdb://datasets/{dataverse}` | Dataset summaries within one dataverse. |
| `asterixdb://indexes/{dataverse}/{dataset}` | Detailed secondary indexes on one dataset. |
| `asterixdb://indexes/{dataverse}` | Detailed secondary index inventory for a dataverse. |

### Prompts

| Prompt | Purpose |
|--------|---------|
| `analyze_dataverse` | Bootstraps exploration with inventory + safety rules. |
| `build_aggregation_query` | Guides building a `GROUP BY` / aggregation query. |
| `analyze_query_performance` | Walks plan + index analysis for a slow query. |
| `recommend_indexes` | Scaffolds the index-suggestion workflow (the `recommend_indexes` tool computes it). |
| `explore_nested_data` | Navigates nested / ROW vs COLUMNAR structures. |
| `explain_error` | Turns a gateway/CC error into a fix. |

Every query carries a namespaced `client_context_id`
(`{agentSessionId}::{userTag}::{uuid}`) for end-to-end auditability, and is bounded
by layered egress controls: a wall-clock timeout, a buffered-response byte ceiling,
and row/byte caps on what reaches the LLM.

## Requirements

- Python 3.10+
- MCP Python SDK 2.x (`mcp>=2.0,<3`, installed as a dependency)
- A reachable AsterixDB cluster (default `http://localhost:19002`)

The SDK serves two protocol eras and the gateway supports both: a classic
`initialize` handshake, which negotiates up to **2025-11-25**, and a modern era at
**2026-07-28**. Existing clients keep working unchanged; the newer revision is
there for clients that ask for it.

`asterixdb://version` reports the gateway version alongside the highest revision
it can speak (`2026-07-28`). Read that field as a ceiling — a client connected
over `initialize` is answered with the handshake-era number instead, which is
correct rather than a mismatch. The value is read from the SDK rather than
declared here, so the gateway can never advertise a revision it does not
implement.

## Install

```bash
git clone https://github.com/<your-fork>/asterixdb-mcp-server.git
cd asterixdb-mcp-server
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Upgrading an existing checkout

The SDK floor moved from 1.x to 2.x, which is a breaking dependency change.
Pulling without reinstalling leaves the environment on the old SDK and the server
will fail to import:

```bash
git pull
pip install -e ".[dev]"     # required, not optional, across the 1.x -> 2.x move
```

The wire format is unaffected — the tools block is byte-identical across the
migration, so connected clients need no change and prompt caching is preserved.

## Configure

All settings come from environment variables (prefix `ASTERIXDB_MCP_`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `ASTERIXDB_MCP_CC_BASE_URL` | `http://localhost:19002` | CC REST base URL. |
| `ASTERIXDB_MCP_CC_SHARED_SECRET` | _(unset)_ | Optional `X-Gateway-Secret` header on the CC hop. |
| `ASTERIXDB_MCP_AGENT_SESSION_ID` | `local-session` | Session-id prefix; each gateway process appends a unique suffix so repeated launches count as distinct sessions in distillation. |
| `ASTERIXDB_MCP_MAX_TIME_MS` | `30000` | Egress layer 1: per-query wall-clock ceiling. |
| `ASTERIXDB_MCP_MAX_BYTES_PER_QUERY` | `10485760` | Egress layer 2: max response bytes buffered. |
| `ASTERIXDB_MCP_REQUEST_TIMEOUT_S` | `35.0` | httpx transport timeout for the CC hop. |
| `ASTERIXDB_MCP_MEMORY_ENABLED` | `true` | Master switch for the memory surface: `false` hides the memory tools and disables the briefing, ambient recall, capture, maintenance, and distill loop. Overrides the write flag. |
| `ASTERIXDB_MCP_MEMORY_WRITE_ENABLED` | `false` | Allow writes to the `AgentMemory` store (`memory_write`, `remember_preference`, capture, maintenance). Every other statement stays read-only. |
| `ASTERIXDB_MCP_SESSION_LOG_DIR` | _(unset)_ | Directory for per-session JSONL query-outcome logs (offline buffer / episodic record). Unset disables file logging. |
| `ASTERIXDB_MCP_AUTO_MAINTENANCE_ENABLED` | `true` | Run the bounded startup maintenance pass (store bootstrap, catalog walk, decay, one distill). Acts only with memory writes enabled. |
| `ASTERIXDB_MCP_DISTILL_INTERVAL_S` | `0` | HTTP transport only: auto-distill period in seconds; `0` disables the loop. Requires memory writes enabled. |

### HTTP transport (optional)

The gateway speaks **stdio** by default (a local sidecar). Set `transport=http` to
expose the MCP **Streamable HTTP** endpoint for remote / multi-client / web access.

| Variable | Default | Meaning |
|----------|---------|---------|
| `ASTERIXDB_MCP_TRANSPORT` | `stdio` | `stdio` or `http`. |
| `ASTERIXDB_MCP_HTTP_HOST` | `127.0.0.1` | Bind host. Keep loopback unless behind a proxy. |
| `ASTERIXDB_MCP_HTTP_PORT` | `19200` | Bind port (AsterixDB `19xxx` family, clear of the cluster's own ports). |
| `ASTERIXDB_MCP_HTTP_PATH` | `/mcp` | Streamable HTTP endpoint path. |
| `ASTERIXDB_MCP_AUTH_MODE` | `none` | `none` (loopback only), `bearer`, or `oauth`. |
| `ASTERIXDB_MCP_API_KEY` | _(unset)_ | Bearer token for `auth_mode=bearer` (≥ 16 chars). |
| `ASTERIXDB_MCP_OAUTH_ISSUER` | _(unset)_ | Authorization-server issuer URL (`auth_mode=oauth`). |
| `ASTERIXDB_MCP_OAUTH_AUDIENCE` | _(unset)_ | This server's audience (token `aud`, RFC 8707). |
| `ASTERIXDB_MCP_OAUTH_JWKS_URI` | _(unset)_ | AS JWKS endpoint for token-signature verification. |
| `ASTERIXDB_MCP_OAUTH_REQUIRED_SCOPES` | `[]` | Scopes a token must carry (JSON list). |
| `ASTERIXDB_MCP_OAUTH_ALGORITHMS` | `["RS256"]` | Accepted JWT signing algorithms (JSON list). |
| `ASTERIXDB_MCP_HTTP_ALLOWED_HOSTS` | `[]` | Extra `Host` values to allow (proxy host; include `:port` when non-default). |
| `ASTERIXDB_MCP_HTTP_ALLOWED_ORIGINS` | `[]` | Extra `Origin` values to allow (browser origin, scheme + host[:port]). |

A `GET /health` liveness probe is served unauthenticated and returns
`{"status":"ok"}` (no cluster call, no version disclosure).

#### Security model

The HTTP listener is built to a defensive baseline:

- **DNS-rebinding protection** is always on for HTTP: only the gateway's own
  `host:port` (plus loopback and any configured extras) is accepted in the `Host`
  and `Origin` headers, so a browser page cannot drive a localhost gateway.
- **Auth is required off loopback.** `auth_mode=none` is refused on a non-loopback
  bind — the server fails fast rather than exposing the database.
- **`bearer`** is a shared static token (constant-time compared); minimum 16 chars.
- **`oauth`** makes the gateway an **OAuth 2.1 resource server**: it verifies bearer
  JWTs against your authorization server's JWKS and checks issuer, audience, expiry,
  and required scopes. It never issues tokens — bring an external AS (Auth0,
  Keycloak, WorkOS, Okta, …). Clients discover the AS via
  `/.well-known/oauth-protected-resource`.
- **Terminate TLS at a reverse proxy.** The server speaks plaintext HTTP; never
  send a bearer token over an unencrypted public hop. Bind loopback and front it
  with a TLS-terminating proxy for any non-local deployment.
- The read-only guarantee is unaffected: `readonly=true` is still forced on every
  CC query regardless of transport or auth.

> Bearer is a pragmatic tier for a gateway behind a trusted proxy; `oauth` is the
> spec-aligned model with rotation, audience binding, and per-client identity.

## Run

```bash
asterixdb-mcp-server        # serves MCP over stdio (default)

# Streamable HTTP on 127.0.0.1:19200 with OAuth 2.1 resource-server auth:
ASTERIXDB_MCP_TRANSPORT=http \
ASTERIXDB_MCP_AUTH_MODE=oauth \
ASTERIXDB_MCP_OAUTH_ISSUER=https://your-as.example.com \
ASTERIXDB_MCP_OAUTH_AUDIENCE=https://mcp.example.com/mcp \
ASTERIXDB_MCP_OAUTH_JWKS_URI=https://your-as.example.com/.well-known/jwks.json \
  asterixdb-mcp-server
```

### Connect Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "asterixdb": {
      "command": "/absolute/path/to/.venv/bin/asterixdb-mcp-server",
      "env": { "ASTERIXDB_MCP_CC_BASE_URL": "http://localhost:19002" }
    }
  }
}
```

### Any MCP client

The server speaks MCP over stdio, so any MCP-capable client works: launch the
`asterixdb-mcp-server` console script as the server command and set
`ASTERIXDB_MCP_CC_BASE_URL` to your cluster. The model behind the client is your
choice — the gateway is model-agnostic and holds no provider keys.

### Route memory to the shared store

Agent hosts ship their own memory features (rules files, "learn" commands,
scratchpads) that intercept phrases like "remember this" before the model ever
weighs the gateway's tools. Those host memories are private to one client on one
machine; the gateway's store is shared across every session and every client
connected to the database. To make the shared store win, add one rule to the
client's custom-instructions / rules surface:

> For anything about this database, use the asterixdb MCP tools — including
> remembering: store facts with memory_write and query-writing rules with
> remember_preference, never with your own memory feature or notes files.

The deterministic paths (learned notes attached to schemas, samples, first
queries, and failures; automatic error→fix capture) work without this rule; the
rule is what routes explicit "remember ..." requests correctly.

## Develop

```bash
ruff check src tests        # lint
ruff format src tests       # format
mypy                        # strict type-check
coverage run -m pytest      # unit + contract tests
coverage report             # enforces 100% line+branch coverage (fail_under=100)
```

Coverage policy: **100%** line and branch coverage is required. It is enforced by
`fail_under = 100` in `pyproject.toml`, so `coverage report` exits non-zero below it.

## Project layout

```
src/asterixdb_mcp/
  config.py          # env-driven settings
  context_id.py      # {session}::{tag}::{uuid} namespace transform
  errors.py          # error taxonomy + CC-error classification
  egress.py          # layered egress controls (timeout, byte ceiling, row caps)
  cc_client.py       # async CC REST client (readonly=true hardcoded)
  permits.py         # non-blocking concurrency permit pools
  statement_guard.py # pre-flight read-only statement guard
  plan_guard.py      # plan-layer mutation backstop
  server.py          # MCP server binding + transport selection (stdio | http)
  http_app.py        # Streamable HTTP ASGI app, /health probe, bearer middleware
  http_security.py   # DNS-rebinding allowlist, startup checks, oauth wiring
  auth.py            # OAuth 2.1 resource-server JWT verification (JWKS)
  tools/             # one module per tool (SDK-agnostic cores)
  resources/         # live cluster resources + SQL++ reference docs
  prompts/           # guided multi-step workflows
scripts/
  okf_refresh.py     # write path: materialize okf_catalog() into AgentMemory.Memory (bi-temporal);
                     #   --ground executes each doc's profiling queries + native ADVISE
  okf_bundle.py      # export/import the store as an OKF bundle (index.md, log.md, concepts)
  okf_consolidate.py # sleep-time pass: dedup, trust decay, forgetting (supersede below floor)
  memory_eval.py     # eval harness: recall, forgetting, efficiency, reuse metrics from JSONL cases
  cases.jsonl        # starter evaluation corpus for memory_eval.py
  memory_distill.py  # distillation of session events (cluster + JSONL): proven queries + recurring failures
  memory_migrate_labels.py # one-time: label pre-evidence overlay notes as (unverified)
tests/
  unit/              # per-module unit tests
  contract/          # advertised MCP surface
```

## License

Apache-2.0.
