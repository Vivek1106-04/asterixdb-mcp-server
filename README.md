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
LLM client  ──MCP (stdio)──▶  AsterixDB MCP Gateway  ──HTTP──▶  AsterixDB CC
                                (this repo)                     /query/service
                                                                /admin/*
```

## Capabilities

**19 tools, 11 resources, 6 prompts.** Tools perform actions; resources expose
read-only context a client can attach to a session; prompts are guided
multi-step workflows.

Every tool advertises MCP behavioral annotations (`readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`) so a client can tell a
safe read from a state-changing call without parsing the description — the whole
surface is read-only except `cancel_query`, and nothing is destructive. Prompt
and resource-template arguments support live `completion/complete`: typing a
`dataverse`, `dataset`, or grouping/metric field completes from the cluster's
real metadata, scoped by any argument already chosen.

### Tools

| Group | Tool | Purpose |
|-------|------|---------|
| Query | `execute_query` | Synchronous read-only SQL++ with offset/limit windowing. |
| Query | `submit_async_query` | Submit a long-running query; returns a handle. |
| Query | `wait_on_async_query` | Long-poll an async handle to completion. |
| Query | `fetch_query_result` | Page through a completed async result set. |
| Query | `cancel_query` | Cancel an in-flight async query. |
| Analyze | `validate_syntax` | Compile-only syntax check, no execution. |
| Analyze | `explain_query` | Optimizer plan for a statement. |
| Analyze | `check_index_usage` | Whether a query's predicates hit an index. |
| Discover | `list_dataverses` | Enumerate dataverses on the cluster. |
| Discover | `list_datasets` | Paginated dataset discovery, optionally scoped to a dataverse. |
| Discover | `describe_dataverse` | Datasets, types, indexes, and functions in one dataverse. |
| Discover | `get_schema` | Single-dataset schema incl. `datasetFormatInfo` (ROW/COLUMNAR). |
| Discover | `sample_dataset` | A small bounded row sample from a dataset. |
| Discover | `search_metadata` | Cross-metadata search for datasets/types/indexes/functions. |
| Functions | `list_functions` | Built-in / user-defined functions, filtered by language. |
| Functions | `get_function` | One function's signature, with near-name hints on a miss. |
| Cluster | `get_cluster_status` | Live cluster state and node roster. |
| Cluster | `get_node_details` | Per-node diagnostics for a validated node id. |
| Docs | `get_reference` | SQL++ reference docs by topic. |

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

### Prompts

| Prompt | Purpose |
|--------|---------|
| `analyze_dataverse` | Bootstraps exploration with inventory + safety rules. |
| `build_aggregation_query` | Guides building a `GROUP BY` / aggregation query. |
| `analyze_query_performance` | Walks plan + index analysis for a slow query. |
| `recommend_indexes` | Suggests indexes from a query workload. |
| `explore_nested_data` | Navigates nested / ROW vs COLUMNAR structures. |
| `explain_error` | Turns a gateway/CC error into a fix. |

Every query carries a namespaced `client_context_id`
(`{agentSessionId}::{userTag}::{uuid}`) for end-to-end auditability, and is bounded
by layered egress controls: a wall-clock timeout, a buffered-response byte ceiling,
and row/byte caps on what reaches the LLM.

## Requirements

- Python 3.10+
- A reachable AsterixDB cluster (default `http://localhost:19002`)

## Install

```bash
git clone https://github.com/<your-fork>/asterixdb-mcp-server.git
cd asterixdb-mcp-server
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configure

All settings come from environment variables (prefix `ASTERIXDB_MCP_`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `ASTERIXDB_MCP_CC_BASE_URL` | `http://localhost:19002` | CC REST base URL. |
| `ASTERIXDB_MCP_CC_SHARED_SECRET` | _(unset)_ | Optional `X-Gateway-Secret` header on the CC hop. |
| `ASTERIXDB_MCP_AGENT_SESSION_ID` | `local-session` | Leading segment of the namespaced context id. |
| `ASTERIXDB_MCP_MAX_TIME_MS` | `30000` | Egress layer 1: per-query wall-clock ceiling. |
| `ASTERIXDB_MCP_MAX_BYTES_PER_QUERY` | `10485760` | Egress layer 2: max response bytes buffered. |
| `ASTERIXDB_MCP_REQUEST_TIMEOUT_S` | `35.0` | httpx transport timeout for the CC hop. |

## Run

```bash
asterixdb-mcp-server        # serves MCP over stdio
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
  server.py          # FastMCP binding (19 tools, 11 resources, 6 prompts)
  tools/             # one module per tool (SDK-agnostic cores)
  resources/         # live cluster resources + SQL++ reference docs
  prompts/           # guided multi-step workflows
tests/
  unit/              # per-module unit tests
  contract/          # advertised MCP surface
```

## License

Apache-2.0.
