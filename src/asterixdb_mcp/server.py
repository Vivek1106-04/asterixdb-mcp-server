"""FastMCP server: binds the tool/resource/prompt surface to MCP.

All AsterixDB-facing logic lives in the tools, resources and prompts packages as
SDK-agnostic run_* functions. This module is the thin adapter: it owns the httpx
client lifecycle, derives the MCP input schemas from the wrapper signatures, and
turns a ToolResult into a CallToolResult.

Public argument names are camelCase (e.g. compilerParameters) to match the
LLM-facing contract.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, cast

import httpx
from mcp import types
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .audit_log import AuditLog
from .cc_client import CCClient
from .completions import complete_argument
from .config import Settings, load_settings
from .errors import GatewayError
from .http_security import build_auth, build_transport_security, validate_http_security
from .output_schemas import apply_output_schemas
from .permits import PermitPools
from .prompts.analyze_dataverse import run_analyze_dataverse
from .prompts.power_prompts import (
    compose_analyze_query_performance,
    compose_build_aggregation_query,
    compose_explain_error,
    compose_explore_nested_data,
    compose_recommend_indexes,
)
from .resources.cluster_diagnostics import read_cluster_diagnostics
from .resources.cluster_status import read_cluster_status
from .resources.config_parameters import read_config_parameters
from .resources.dataverses import read_dataverses
from .resources.reference import (
    read_builtin_functions,
    read_error_codes,
    read_index_types,
    read_query_examples,
    read_sqlpp_syntax,
    read_type_system,
)
from .resources.templates import (
    read_dataset_indexes,
    read_dataset_sample,
    read_dataset_schema,
    read_dataverse_datasets,
    read_dataverse_indexes,
    read_dataverse_schema,
)
from .resources.version import read_version
from .tool_annotations import TOOL_ANNOTATIONS
from .tools import ToolResult
from .tools.async_query import (
    run_cancel_query,
    run_fetch_query_result,
    run_submit_async_query,
    run_wait_on_async_query,
)
from .tools.check_index_usage import run_check_index_usage
from .tools.describe_dataverse import run_describe_dataverse
from .tools.execute_query import run_execute_query
from .tools.functions import run_get_function, run_list_functions
from .tools.get_cluster_status import run_get_cluster_status
from .tools.get_node_details import run_get_node_details
from .tools.get_reference import run_get_reference
from .tools.get_schema import run_get_schema
from .tools.health_check import run_database_health_check
from .tools.introspect import run_explain_query, run_validate_syntax
from .tools.list_datasets import run_list_datasets
from .tools.list_dataverses import run_list_dataverses
from .tools.query_history import record_query, run_get_query_history
from .tools.recommend_indexes import run_recommend_indexes
from .tools.sample_dataset import run_sample_dataset
from .tools.search_metadata import run_search_metadata

EXECUTE_QUERY_DESCRIPTION = (
    "Execute a read-only SQL++ query against Apache AsterixDB SYNCHRONOUSLY.\n\n"
    "CRITICAL RULES:\n"
    "1. You MUST include a LIMIT clause in every SELECT query. Start with LIMIT 20 and "
    "increase only if the user explicitly requests more data. Queries without LIMIT may be "
    "terminated by the server.\n"
    "2. This tool is READ-ONLY. All INSERT, UPSERT, DELETE, DROP, CREATE, and LOAD statements "
    "will be rejected by the database.\n"
    "3. Qualify dataset references with their Dataverse name (e.g. `MyDataverse.MyDataset`) "
    "unless you set the default Dataverse via the `dataverse` parameter.\n"
    "4. AsterixDB uses SQL++. Use backticks for reserved-word identifiers, `VALUE` for "
    "single-expression SELECT, dot-notation for nested fields.\n"
    "5. STORAGE FORMAT AWARENESS: call get_schema first. If `datasetFormatInfo.format` is "
    "COLUMNAR, never write SELECT *; always project explicit fields.\n"
    "6. FIELD NAMES: before referencing ANY field in SELECT, WHERE, JOIN, or GROUP BY, call "
    "get_schema (or describe_dataverse) and copy field names with their EXACT casing. Do not "
    "guess or normalize casing — AsterixDB uses the stored name verbatim (e.g. it may be "
    "snake_case, not camelCase). A JOIN or filter on a misspelled field does NOT error: the "
    "field reads as MISSING, and `MISSING = MISSING` never matches, so the query silently "
    "returns 0 rows. If a query that should match returns 0 rows, re-check field names against "
    "get_schema before changing anything else.\n"
    "7. Do NOT prepend `SET ...` to the statement; pass tuning knobs via `compilerParameters`.\n"
    "8. CHECK BEFORE YOU RUN: if the statement is non-trivial (built-in functions you are not "
    "certain exist, nested or multiple aggregates, JOINs, GROUP BY), call validate_syntax "
    "first and fix until it returns `valid:true`. validate_syntax is compile-only and near-"
    "instant; do NOT use execute_query as a syntax checker. Note AsterixDB aggregate names: "
    "standard deviation is `STDDEV_SAMP`/`STDDEV_POP`, variance is `VAR_SAMP`/`VAR_POP` (there "
    "is no `STDEV`). Do not nest an aggregate inside another aggregate in the same SELECT."
)

GET_SCHEMA_DESCRIPTION = (
    "Retrieve the declared schema for a SINGLE Dataset within a Dataverse: declared type, "
    "primary key, field definitions, secondary indexes, and `datasetFormatInfo` (ROW vs "
    "COLUMNAR storage). Call list_datasets first to discover names. ALWAYS read "
    "`datasetFormatInfo.format` before writing a SELECT. COLUMNAR datasets require explicit "
    "field projection."
)

LIST_DATAVERSES_DESCRIPTION = (
    "List every dataverse (namespace) on the cluster. The top-level discovery primitive: "
    "call this FIRST to learn which dataverses exist before using list_datasets or get_schema. "
    "Takes no arguments."
)

LIST_DATASETS_DESCRIPTION = (
    "List datasets, optionally scoped to one Dataverse, with offset/limit pagination. Returns "
    "a cheap summary per dataset (name, datatype, type, storage format). The discovery "
    "primitive: use it to find Dataset names to pass to get_schema."
)

ANALYZE_DATAVERSE_DESCRIPTION = (
    "Bootstrap a Dataverse exploration session with the dataset inventory, storage-format "
    "awareness rules, and the read-only safety contract embedded in context."
)

DESCRIBE_DATAVERSE_DESCRIPTION = (
    "Return the full schema of every dataset in a Dataverse in a single call: declared "
    "fields, primary keys, secondary indexes, and storage format per dataset. Use this to "
    "understand or explain an entire Dataverse at once instead of calling get_schema "
    "repeatedly. The Dataverse name is resolved case-insensitively."
)

SAMPLE_DATASET_DESCRIPTION = (
    "Retrieve a small sample of real documents from a dataset (a bounded SELECT VALUE ... "
    "LIMIT N). Use it before writing a filter to see how values are actually stored "
    "(encodings, casing, formats, units) and to discover undeclared fields on OPEN "
    "datasets. Declared types from get_schema are authoritative for known fields; sampling "
    "reveals the real values. Dataverse and dataset names are resolved case-insensitively."
)

SUBMIT_ASYNC_QUERY_DESCRIPTION = (
    "Submit a read-only SQL++ query for ASYNCHRONOUS execution and return immediately with a "
    "`clientContextID` and a status `handle`. Use this for queries you expect to run long "
    "(large scans, heavy aggregations) instead of blocking on execute_query.\n\n"
    "ALL execute_query rules apply here too — most importantly, call get_schema on every "
    "dataset you reference and copy field names with their EXACT casing BEFORE you write the "
    "query. A JOIN on a misspelled key silently returns 0 rows.\n\n"
    "CHECK BEFORE YOU SUBMIT: an async submission is expensive. If the statement is non-"
    "trivial (uncertain function names, nested/multiple aggregates, JOINs, GROUP BY), call "
    "validate_syntax first and fix until `valid:true` — it is compile-only and near-instant. "
    "Do NOT use submit_async_query to discover syntax errors. AsterixDB standard deviation is "
    "`STDDEV_SAMP`/`STDDEV_POP` (no `STDEV`); never nest an aggregate inside another aggregate "
    "in the same SELECT.\n\n"
    "LIFECYCLE (you MUST complete it — submitting alone is NOT an answer): "
    "1) submit_async_query returns a `clientContextID` — the ONE id for the whole lifecycle; "
    "2) call wait_on_async_query with that clientContextID, and call it again while it returns "
    "`done:false`, until `done:true`; "
    "3) call fetch_query_result with the SAME clientContextID and report the actual rows. "
    "Always pass the clientContextID back exactly as returned; never pass a URL or handle. "
    "Use cancel_query (same clientContextID) only to abort. Pass tuning knobs via "
    "`compilerParameters` (validated against asterixdb://config-parameters)."
)

WAIT_ON_ASYNC_QUERY_DESCRIPTION = (
    "Long-poll a submitted query for up to `timeoutMs` milliseconds (bounded by the gateway). "
    "Pass the SAME `clientContextID` that submit_async_query returned — not a URL or handle. "
    "Returns `done:true` when the query has succeeded (then call fetch_query_result with the "
    "same clientContextID), an error when it failed, or `done:false` if it is still running "
    "(call this again with the same clientContextID to keep waiting). Does not hold a database "
    "connection open across calls."
)

FETCH_QUERY_RESULT_DESCRIPTION = (
    "Fetch the rows of a completed async query. Pass the SAME `clientContextID` that "
    "submit_async_query returned. Supports offset/limit windowing. Call this only after "
    "wait_on_async_query has reported `done:true` for that clientContextID."
)

CANCEL_QUERY_DESCRIPTION = (
    "Cancel a still-running query by the same `clientContextID` that submit_async_query "
    "returned. Returns NOT_FOUND if the query already finished or was already cancelled."
)

VALIDATE_SYNTAX_DESCRIPTION = (
    "Compile a SQL++ statement WITHOUT running it to check whether it is valid. Returns "
    "`valid:true` on success, or `valid:false` with an errorType that distinguishes a "
    "SYNTAX_ERROR (malformed SQL++) from a SEMANTIC_ERROR (unknown dataset/field, type "
    "mismatch). Use it to check a query cheaply before executing it."
)

EXPLAIN_QUERY_DESCRIPTION = (
    "Compile a SQL++ statement WITHOUT running it and return its optimized logical plan as a "
    "structured operator tree: operator kinds and counts, the datasets scanned, predicates, "
    "and plan depth. Use it to understand how a query will execute and which access paths it "
    "uses before paying to run it."
)

CHECK_INDEX_USAGE_DESCRIPTION = (
    "Analyze whether a SQL++ query uses the secondary indexes available on the datasets it "
    "touches. Compile-only and read-only: it returns `used` (indexes the optimized plan "
    "actually uses), `availableButUnused` (indexes that exist but the plan ignores), and "
    "`usesFullScan`. Use it to decide whether a slow query needs a different predicate or a "
    "new index. Pass a complete SELECT; qualify names or set `dataverse`."
)

LIST_FUNCTIONS_DESCRIPTION = (
    "List SQL++ functions — both built-ins and user-defined functions (UDFs) — so you never "
    "guess a function name. Built-ins are read live from the engine, so the list reflects the "
    "running version. Filter by `language` (INTERNAL = built-in, SQL++/JAVA/PYTHON = UDF), by "
    "`category` for built-ins (window, aggregate, aggregate-scalar, unnest, datasource, scalar), "
    "and/or a `nameContains` substring, with offset/limit paging. Use `category=window` to find "
    "window functions and `category=aggregate` for the SQL++ aggregates. Call this before using "
    "an unfamiliar function; the built-in standard-deviation aggregates are STDDEV_SAMP and "
    "STDDEV_POP (there is no STDEV)."
)

GET_FUNCTION_DESCRIPTION = (
    "Get one function's details by `name`: a built-in returns its category and summary; a UDF "
    "(give its `dataverse`) returns its signature, return type, and body. External Java/Python "
    "UDFs are flagged because their body runs code on the cluster. Returns NOT_FOUND with "
    "guidance if the name is unknown — call list_functions to discover exact names."
)

SEARCH_METADATA_DESCRIPTION = (
    "Fuzzy-search the metadata catalog by NAME across datasets, datatypes, indexes, functions, "
    "synonyms, and feeds. Returns the closest-matching objects ranked by similarity (exact > "
    "prefix > substring > approximate). Use it to answer 'is there something named like X?' "
    "without inventing names; then call get_schema/get_function for the exact object."
)

GET_CLUSTER_STATUS_DESCRIPTION = (
    "Get the AsterixDB version, overall cluster state, and the per-node roster (node ids and "
    "their states) in one call. Use this to answer cluster health/version questions and to "
    "discover the node ids that get_node_details needs. Takes no arguments."
)

GET_NODE_DETAILS_DESCRIPTION = (
    "Get per-node-controller statistics for a single node by `node` id. Call get_cluster_status "
    "first to obtain valid node ids. Returns NOT_FOUND if the node id is unknown."
)

GET_REFERENCE_DESCRIPTION = (
    "Read curated AsterixDB SQL++ reference material to ground yourself BEFORE writing queries: "
    "syntax rules, the type system, index types, common error codes, worked query examples, and "
    "the built-in function catalog. Pass a single `topic`, or `all` to retrieve every topic at "
    "once. This is the authoritative in-gateway documentation — prefer it over guessing syntax."
)

DATABASE_HEALTH_CHECK_DESCRIPTION = (
    "Scan the metadata catalog for schema-level health issues and return ranked findings. "
    "Read-only and metadata-only (no workload is run): it reports DUPLICATE_INDEX (two secondary "
    "indexes with the same structure and key fields — drop the extra), REDUNDANT_INDEX (an index "
    "whose key fields are a prefix of a longer same-structure index), and "
    "ROW_DATASET_COLUMNAR_CANDIDATE (an internal ROW dataset that may benefit from COLUMNAR "
    "storage for analytical scans). Optionally scope to one `dataverse`; the system Metadata "
    "dataverse is never reported. Each finding carries a severity (high/medium/low) and a plain "
    "fix. Workload-driven advice (unused indexes, un-indexed filtered fields) is NOT covered "
    "here — use get_query_history and the recommend_indexes prompt for that."
)

GET_QUERY_HISTORY_DESCRIPTION = (
    "List the queries run in THIS session, newest first, with their outcome — for self-"
    "debugging. Each entry has the tool used, the statement, the outcome (SUCCESS/ERROR/"
    "SUBMITTED), and on failure the classified errorType and message. Set `failuresOnly:true` to "
    "see only the calls that failed, e.g. to recall the exact error before retrying. Reads "
    "in-gateway memory only (no cluster call); the history is session-scoped and expires with the "
    "audit-log TTL, so it shows recent activity rather than a full log."
)

RECOMMEND_INDEXES_DESCRIPTION = (
    "Recommend secondary indexes for a WORKLOAD of representative SQL++ SELECTs. Read-only: it "
    "runs AsterixDB's NATIVE cost-based index advisor (`ADVISE`) on each statement — the engine "
    "costs hypothetical indexes through its optimizer and returns the indexes it would create — "
    "and aggregates the `recommended_indexes` across the workload, ranked by how many statements "
    "want each. When a cluster does not support `ADVISE`, it falls back to a heuristic plan scan. "
    "`method` reports which path was used; `currentIndexes` lists indexes already present. Each "
    "recommendation carries a `recommendedDDL` (CREATE INDEX ...) and a `confidence`; the gateway "
    "is READ-ONLY and will NOT run the DDL — it is advice for you or an operator to apply. A "
    "statement neither path can use is reported in `skipped` rather than failing the batch. Pass "
    "complete SELECTs; qualify names or set `dataverse`. For a single slow query, prefer "
    "check_index_usage."
)


# The FastMCP.completion() decorator ships unannotated, so mypy flags every
# handler it wraps as untyped. The handler IS fully typed; the gap is in the SDK.
# A narrow Protocol describes the typed shape of completion() and a cast applies
# it at the single registration site, keeping the boundary honest without a blanket
# ignore. The other decorators (tool/resource/prompt) are typed in the SDK already.
_CompletionHandler = Callable[
    [
        types.PromptReference | types.ResourceTemplateReference,
        types.CompletionArgument,
        types.CompletionContext | None,
    ],
    Awaitable[types.Completion | None],
]


class _SupportsCompletion(Protocol):
    """The typed slice of FastMCP we rely on for completion registration."""

    def completion(self) -> Callable[[_CompletionHandler], _CompletionHandler]: ...


@dataclass
class _ClientHolder:
    """Holds the CC client so tool closures share one instance.

    A test may seed an injected client; otherwise one is created lazily on first
    use, bound to the configured cluster URL. The httpx connection pool lives for
    the process lifetime and is released on exit. A stdio server runs until the
    client disconnects, so there is no separate teardown phase to manage.
    """

    settings: Settings
    client: CCClient | None = None

    def get(self) -> CCClient:
        if self.client is None:
            self.client = CCClient(
                self.settings,
                httpx.AsyncClient(
                    base_url=self.settings.cc_base_url,
                    timeout=self.settings.request_timeout_s,
                ),
            )
        return self.client


def _to_call_tool_result(result: ToolResult) -> types.CallToolResult:
    """Convert an SDK-agnostic ToolResult to an MCP CallToolResult.

    Two MCP realities shape this:

    1. Many clients (e.g. Antigravity) render only ``content[].text`` and ignore
       ``structuredContent``. If the payload lived only in structured content,
       those clients would see a bare summary like "Returned 5 row(s)." and no
       rows. So on success the structured payload is also serialized into the text
       block, making every tool usable by text-first AND structured clients alike.
       The payload is already egress-bounded (rows/bytes-to-LLM caps), so this
       cannot blow up the context window.
    2. A client validates ``structuredContent`` against the tool's advertised
       ``outputSchema``, which describes a *successful* result. An error envelope
       has none of that shape, so sending it as structured content makes a
       validating client reject the call and mask the real classified error.
       Errors therefore carry no structured content and no JSON mirror; the
       ``errorType`` and message live in the text content alone.
    """
    if result.is_error:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.text)],
            structuredContent=None,
            isError=True,
        )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_text_with_payload(result))],
        structuredContent=result.structured,
        isError=False,
    )


def _text_with_payload(result: ToolResult) -> str:
    """Summary text plus the structured payload as a fenced JSON block.

    Mirrors the machine-readable result into the human/LLM-readable text so
    clients that ignore ``structuredContent`` still receive the full data.
    """
    if not result.structured:
        return result.text
    # Compact separators keep the mirrored text aligned with the egress byte
    # budget, which accounts for rows as compact JSON; indenting would inflate the
    # size past the intended max-bytes-to-LLM ceiling.
    payload = json.dumps(result.structured, default=str, ensure_ascii=False, separators=(",", ":"))
    return f"{result.text}\n\n```json\n{payload}\n```"


def build_server(settings: Settings, http: httpx.AsyncClient | None = None) -> FastMCP:
    """Construct the FastMCP app and register the tool/resource/prompt surface.

    Args:
        settings: Loaded gateway settings.
        http: Optional injected httpx client (tests pass a MockTransport-backed
            client). When omitted, a client is created and disposed by the
            FastMCP lifespan bound to the cluster base URL.
    """
    holder = _ClientHolder(
        settings=settings,
        client=CCClient(settings, http) if http is not None else None,
    )
    audit = AuditLog(settings.audit_log_ttl_s)
    pools = PermitPools.from_settings(settings)

    # Host/port/path are read by the Streamable HTTP transport (transport='http');
    # they are inert under stdio. For http we also fail fast on an unsafe config,
    # enable DNS-rebinding protection, and wire oauth resource-server auth.
    mcp_kwargs: dict[str, Any] = {
        "host": settings.http_host,
        "port": settings.http_port,
        "streamable_http_path": settings.http_path,
    }
    if settings.transport == "http":
        validate_http_security(settings)
        mcp_kwargs["transport_security"] = build_transport_security(settings)
        auth, token_verifier = build_auth(settings)
        if auth is not None:
            mcp_kwargs["auth"] = auth
            mcp_kwargs["token_verifier"] = token_verifier
    mcp = FastMCP("asterixdb-mcp-server", **mcp_kwargs)

    def _client() -> CCClient:
        return holder.get()

    # Tools

    @mcp.tool(
        name="execute_query",
        description=EXECUTE_QUERY_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["execute_query"],
    )
    async def execute_query(
        statement: Annotated[str, Field(description="Pure SQL++ statement, no SET prefix.")],
        dataverse: Annotated[
            str | None, Field(description="Default Dataverse for unqualified names.")
        ] = None,
        offset: Annotated[int, Field(ge=0, description="Row window offset.")] = 0,
        limit: Annotated[int, Field(ge=1, le=1000, description="Max rows to return.")] = 20,
        compilerParameters: Annotated[
            dict[str, Any] | None,
            Field(description="Compiler/runtime tuning knobs forwarded as CC form parameters."),
        ] = None,
        profile: Annotated[bool, Field(description="If true, return execution metrics.")] = False,
        signature: Annotated[
            bool, Field(description="If true, return the inferred result-type signature.")
        ] = False,
        maxWarnings: Annotated[
            int, Field(ge=0, le=100, description="Cap on compilation warnings returned.")
        ] = 5,
    ) -> types.CallToolResult:
        # The sync permit pool bounds concurrent blocking queries; a full pool
        # sheds load with NOT_READY rather than queueing.
        try:
            async with pools.sync.acquire():
                result = await run_execute_query(
                    _client(),
                    settings,
                    statement=statement,
                    dataverse=dataverse,
                    offset=offset,
                    limit=limit,
                    compiler_parameters=compilerParameters,
                    profile=profile,
                    signature=signature,
                    max_warnings=maxWarnings,
                )
        except GatewayError as err:
            result = ToolResult.error(err)
        # Record the call's outcome so get_query_history can surface it for
        # self-debugging (success and failure alike).
        record_query(
            audit,
            settings,
            tool="execute_query",
            statement=statement,
            dataverse=dataverse,
            result=result,
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="get_schema",
        description=GET_SCHEMA_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["get_schema"],
    )
    async def get_schema(
        dataverse: Annotated[str, Field(description="Dataverse containing the dataset.")],
        dataset: Annotated[str, Field(description="Dataset to describe.")],
    ) -> types.CallToolResult:
        result = await run_get_schema(_client(), settings, dataverse=dataverse, dataset=dataset)
        return _to_call_tool_result(result)

    @mcp.tool(
        name="list_dataverses",
        description=LIST_DATAVERSES_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["list_dataverses"],
    )
    async def list_dataverses() -> types.CallToolResult:
        result = await run_list_dataverses(_client(), settings)
        return _to_call_tool_result(result)

    @mcp.tool(
        name="list_datasets",
        description=LIST_DATASETS_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["list_datasets"],
    )
    async def list_datasets(
        dataverse: Annotated[str | None, Field(description="Optional Dataverse filter.")] = None,
        offset: Annotated[int, Field(ge=0, description="Page offset.")] = 0,
        limit: Annotated[int, Field(ge=1, le=500, description="Page size.")] = 50,
    ) -> types.CallToolResult:
        result = await run_list_datasets(
            _client(), settings, dataverse=dataverse, offset=offset, limit=limit
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="describe_dataverse",
        description=DESCRIBE_DATAVERSE_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["describe_dataverse"],
    )
    async def describe_dataverse(
        dataverse: Annotated[str, Field(description="Dataverse to describe in full.")],
    ) -> types.CallToolResult:
        result = await run_describe_dataverse(_client(), settings, dataverse=dataverse)
        return _to_call_tool_result(result)

    @mcp.tool(
        name="sample_dataset",
        description=SAMPLE_DATASET_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["sample_dataset"],
    )
    async def sample_dataset(
        dataverse: Annotated[str, Field(description="Dataverse containing the dataset.")],
        dataset: Annotated[str, Field(description="Dataset to sample.")],
        size: Annotated[int, Field(ge=1, le=100, description="Rows to sample.")] = 10,
    ) -> types.CallToolResult:
        result = await run_sample_dataset(
            _client(), settings, dataverse=dataverse, dataset=dataset, size=size
        )
        return _to_call_tool_result(result)

    # Async query lifecycle tools

    @mcp.tool(
        name="submit_async_query",
        description=SUBMIT_ASYNC_QUERY_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["submit_async_query"],
    )
    async def submit_async_query(
        statement: Annotated[str, Field(description="Pure SQL++ statement, no SET prefix.")],
        dataverse: Annotated[
            str | None, Field(description="Default Dataverse for unqualified names.")
        ] = None,
        compilerParameters: Annotated[
            dict[str, Any] | None,
            Field(description="Compiler/runtime tuning knobs forwarded as CC form parameters."),
        ] = None,
    ) -> types.CallToolResult:
        result = await run_submit_async_query(
            _client(),
            settings,
            audit,
            pools,
            statement=statement,
            dataverse=dataverse,
            compiler_parameters=compilerParameters,
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="wait_on_async_query",
        description=WAIT_ON_ASYNC_QUERY_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["wait_on_async_query"],
    )
    async def wait_on_async_query(
        clientContextID: Annotated[
            str, Field(description="clientContextID returned by submit_async_query.")
        ],
        timeoutMs: Annotated[
            int | None,
            Field(ge=0, description="Max milliseconds to wait (clamped by the gateway)."),
        ] = None,
    ) -> types.CallToolResult:
        result = await run_wait_on_async_query(
            _client(),
            settings,
            audit,
            pools,
            client_context_id=clientContextID,
            timeout_ms=timeoutMs,
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="fetch_query_result",
        description=FETCH_QUERY_RESULT_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["fetch_query_result"],
    )
    async def fetch_query_result(
        clientContextID: Annotated[
            str, Field(description="clientContextID returned by submit_async_query.")
        ],
        offset: Annotated[int, Field(ge=0, description="Row window offset.")] = 0,
        limit: Annotated[int, Field(ge=1, le=1000, description="Max rows to return.")] = 20,
    ) -> types.CallToolResult:
        result = await run_fetch_query_result(
            _client(),
            settings,
            audit,
            client_context_id=clientContextID,
            offset=offset,
            limit=limit,
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="cancel_query",
        description=CANCEL_QUERY_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["cancel_query"],
    )
    async def cancel_query(
        clientContextID: Annotated[
            str, Field(description="clientContextID from submit_async_query.")
        ],
    ) -> types.CallToolResult:
        result = await run_cancel_query(
            _client(), settings, audit, client_context_id=clientContextID
        )
        return _to_call_tool_result(result)

    # Introspection tools

    @mcp.tool(
        name="validate_syntax",
        description=VALIDATE_SYNTAX_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["validate_syntax"],
    )
    async def validate_syntax(
        statement: Annotated[str, Field(description="SQL++ statement to compile-check.")],
        dataverse: Annotated[
            str | None, Field(description="Default Dataverse for unqualified names.")
        ] = None,
    ) -> types.CallToolResult:
        result = await run_validate_syntax(
            _client(), settings, statement=statement, dataverse=dataverse
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="explain_query",
        description=EXPLAIN_QUERY_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["explain_query"],
    )
    async def explain_query(
        statement: Annotated[str, Field(description="SQL++ statement to explain.")],
        dataverse: Annotated[
            str | None, Field(description="Default Dataverse for unqualified names.")
        ] = None,
    ) -> types.CallToolResult:
        result = await run_explain_query(
            _client(), settings, statement=statement, dataverse=dataverse
        )
        return _to_call_tool_result(result)

    # Discovery & diagnostics tools

    @mcp.tool(
        name="check_index_usage",
        description=CHECK_INDEX_USAGE_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["check_index_usage"],
    )
    async def check_index_usage(
        statement: Annotated[str, Field(description="SQL++ SELECT to analyze.")],
        dataverse: Annotated[
            str | None, Field(description="Default Dataverse for unqualified names.")
        ] = None,
    ) -> types.CallToolResult:
        result = await run_check_index_usage(
            _client(), settings, statement=statement, dataverse=dataverse
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="list_functions",
        description=LIST_FUNCTIONS_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["list_functions"],
    )
    async def list_functions(
        language: Annotated[
            Literal["INTERNAL", "SQL++", "JAVA", "PYTHON"] | None,
            Field(description="Filter by function language."),
        ] = None,
        category: Annotated[
            Literal["window", "aggregate", "aggregate-scalar", "unnest", "datasource", "scalar"]
            | None,
            Field(description="Filter built-ins by category (e.g. window vs aggregate)."),
        ] = None,
        nameContains: Annotated[
            str | None, Field(description="Case-insensitive name substring filter.")
        ] = None,
        offset: Annotated[int, Field(ge=0, description="Page offset.")] = 0,
        limit: Annotated[int, Field(ge=1, le=200, description="Page size.")] = 50,
    ) -> types.CallToolResult:
        result = await run_list_functions(
            _client(),
            settings,
            language=language,
            category=category,
            name_contains=nameContains,
            offset=offset,
            limit=limit,
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="get_function",
        description=GET_FUNCTION_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["get_function"],
    )
    async def get_function(
        name: Annotated[str, Field(description="Function name (built-in or UDF).")],
        dataverse: Annotated[
            str | None, Field(description="Dataverse of a UDF; omit for built-ins.")
        ] = None,
    ) -> types.CallToolResult:
        result = await run_get_function(_client(), settings, name=name, dataverse=dataverse)
        return _to_call_tool_result(result)

    @mcp.tool(
        name="search_metadata",
        description=SEARCH_METADATA_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["search_metadata"],
    )
    async def search_metadata(
        query: Annotated[str, Field(description="Name to fuzzy-search the catalog for.")],
        limit: Annotated[int, Field(ge=1, le=100, description="Max matches.")] = 20,
    ) -> types.CallToolResult:
        result = await run_search_metadata(_client(), settings, query=query, limit=limit)
        return _to_call_tool_result(result)

    @mcp.tool(
        name="get_cluster_status",
        description=GET_CLUSTER_STATUS_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["get_cluster_status"],
    )
    async def get_cluster_status() -> types.CallToolResult:
        result = await run_get_cluster_status(_client())
        return _to_call_tool_result(result)

    @mcp.tool(
        name="get_node_details",
        description=GET_NODE_DETAILS_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["get_node_details"],
    )
    async def get_node_details(
        node: Annotated[str, Field(description="Node-controller id from cluster status.")],
    ) -> types.CallToolResult:
        result = await run_get_node_details(_client(), settings, node=node)
        return _to_call_tool_result(result)

    @mcp.tool(
        name="get_reference",
        description=GET_REFERENCE_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["get_reference"],
    )
    async def get_reference(
        topic: Annotated[
            Literal[
                "sqlpp-syntax",
                "type-system",
                "index-types",
                "query-examples",
                "error-codes",
                "builtin-functions",
                "all",
            ],
            Field(description="Reference topic to read, or 'all' for every topic."),
        ],
    ) -> types.CallToolResult:
        return _to_call_tool_result(run_get_reference(topic))

    @mcp.tool(
        name="database_health_check",
        description=DATABASE_HEALTH_CHECK_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["database_health_check"],
    )
    async def database_health_check(
        dataverse: Annotated[
            str | None, Field(description="Optional dataverse to scope the scan to.")
        ] = None,
    ) -> types.CallToolResult:
        result = await run_database_health_check(_client(), settings, dataverse=dataverse)
        return _to_call_tool_result(result)

    @mcp.tool(
        name="get_query_history",
        description=GET_QUERY_HISTORY_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["get_query_history"],
    )
    async def get_query_history(
        limit: Annotated[int, Field(ge=1, le=100, description="Max entries to return.")] = 20,
        failuresOnly: Annotated[
            bool, Field(description="If true, return only calls that failed.")
        ] = False,
    ) -> types.CallToolResult:
        result = await run_get_query_history(
            audit, settings, limit=limit, failures_only=failuresOnly
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="recommend_indexes",
        description=RECOMMEND_INDEXES_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS["recommend_indexes"],
    )
    async def recommend_indexes_tool(
        statements: Annotated[
            list[str],
            Field(description="Workload of complete SQL++ SELECT statements to analyze."),
        ],
        dataverse: Annotated[
            str | None, Field(description="Default Dataverse for unqualified names.")
        ] = None,
    ) -> types.CallToolResult:
        result = await run_recommend_indexes(
            _client(), settings, statements=statements, dataverse=dataverse
        )
        return _to_call_tool_result(result)

    # Resources

    @mcp.resource("asterixdb://version", name="AsterixDB Version", mime_type="application/json")
    async def version_resource() -> str:
        return json.dumps(await read_version(_client()), default=str)

    @mcp.resource("asterixdb://cluster/status", name="Cluster Status", mime_type="application/json")
    async def cluster_status_resource() -> str:
        return json.dumps(await read_cluster_status(_client()), default=str)

    @mcp.resource(
        "asterixdb://config-parameters",
        name="Gateway Config Parameters",
        mime_type="application/json",
    )
    async def config_parameters_resource() -> str:
        return json.dumps(read_config_parameters(settings), default=str)

    @mcp.resource("asterixdb://dataverses", name="Dataverses", mime_type="application/json")
    async def dataverses_resource() -> str:
        return json.dumps(await read_dataverses(_client(), settings.agent_session_id), default=str)

    @mcp.resource(
        "asterixdb://cluster/diagnostics",
        name="Cluster Diagnostics",
        mime_type="application/json",
    )
    async def cluster_diagnostics_resource() -> str:
        return json.dumps(await read_cluster_diagnostics(_client()), default=str)

    # Parameterized resource templates (client fills the {variables}; they
    # autocomplete through the completion handler registered below).

    @mcp.resource(
        "asterixdb://schema/{dataverse}/{dataset}",
        name="Dataset Schema",
        mime_type="application/json",
    )
    async def dataset_schema_template(dataverse: str, dataset: str) -> str:
        return await read_dataset_schema(_client(), settings, dataverse=dataverse, dataset=dataset)

    @mcp.resource(
        "asterixdb://dataverse/{dataverse}",
        name="Dataverse Schema",
        mime_type="application/json",
    )
    async def dataverse_schema_template(dataverse: str) -> str:
        return await read_dataverse_schema(_client(), settings, dataverse=dataverse)

    @mcp.resource(
        "asterixdb://sample/{dataverse}/{dataset}",
        name="Dataset Sample",
        mime_type="application/json",
    )
    async def dataset_sample_template(dataverse: str, dataset: str) -> str:
        return await read_dataset_sample(_client(), settings, dataverse=dataverse, dataset=dataset)

    @mcp.resource(
        "asterixdb://datasets/{dataverse}",
        name="Dataverse Datasets",
        mime_type="application/json",
    )
    async def dataverse_datasets_template(dataverse: str) -> str:
        return await read_dataverse_datasets(_client(), settings, dataverse=dataverse)

    @mcp.resource(
        "asterixdb://indexes/{dataverse}/{dataset}",
        name="Dataset Indexes",
        mime_type="application/json",
    )
    async def dataset_indexes_template(dataverse: str, dataset: str) -> str:
        return await read_dataset_indexes(
            _client(), settings, dataverse=dataverse, dataset=dataset
        )

    @mcp.resource(
        "asterixdb://indexes/{dataverse}",
        name="Dataverse Indexes",
        mime_type="application/json",
    )
    async def dataverse_indexes_template(dataverse: str) -> str:
        return await read_dataverse_indexes(_client(), settings, dataverse=dataverse)

    # Static reference resources (no runtime fetch)

    @mcp.resource(
        "asterixdb://reference/sqlpp-syntax", name="SQL++ Syntax", mime_type="application/json"
    )
    async def ref_sqlpp_syntax() -> str:
        return json.dumps(read_sqlpp_syntax(), default=str)

    @mcp.resource(
        "asterixdb://reference/builtin-functions",
        name="Built-in Functions",
        mime_type="application/json",
    )
    async def ref_builtin_functions() -> str:
        return json.dumps(read_builtin_functions(), default=str)

    @mcp.resource(
        "asterixdb://reference/index-types", name="Index Types", mime_type="application/json"
    )
    async def ref_index_types() -> str:
        return json.dumps(read_index_types(), default=str)

    @mcp.resource(
        "asterixdb://reference/type-system", name="Type System", mime_type="application/json"
    )
    async def ref_type_system() -> str:
        return json.dumps(read_type_system(), default=str)

    @mcp.resource(
        "asterixdb://reference/error-codes", name="Error Codes", mime_type="application/json"
    )
    async def ref_error_codes() -> str:
        return json.dumps(read_error_codes(), default=str)

    @mcp.resource(
        "asterixdb://reference/query-examples", name="Query Examples", mime_type="application/json"
    )
    async def ref_query_examples() -> str:
        return json.dumps(read_query_examples(), default=str)

    # Prompts

    # Prompt arguments are all optional: some clients invoke prompts/get without
    # collecting argument values first, and a required arg makes the whole prompt
    # fail there. Each composer degrades to placeholder guidance when an argument
    # is missing, so every prompt is usable in any client.

    @mcp.prompt(name="analyze_dataverse", description=ANALYZE_DATAVERSE_DESCRIPTION)
    async def analyze_dataverse(
        dataverse: str | None = None,
        dataset: str | None = None,
    ) -> str:
        return await run_analyze_dataverse(
            _client(), settings, dataverse=dataverse, dataset=dataset
        )

    @mcp.prompt(
        name="build_aggregation_query",
        description="Scaffold a GROUP BY + HAVING aggregation, columnar-aware.",
    )
    async def build_aggregation_query(
        dataverse: str | None = None,
        dataset: str | None = None,
        group_by: str | None = None,
        metric: str | None = None,
    ) -> str:
        return compose_build_aggregation_query(dataverse, dataset, group_by, metric)

    @mcp.prompt(
        name="analyze_query_performance",
        description="Profile a query and interpret its metrics.",
    )
    async def analyze_query_performance(statement: str | None = None) -> str:
        return compose_analyze_query_performance(statement)

    @mcp.prompt(
        name="recommend_indexes",
        description="Chain check_index_usage into an index recommendation.",
    )
    async def recommend_indexes(dataverse: str | None = None, dataset: str | None = None) -> str:
        return compose_recommend_indexes(dataverse, dataset)

    @mcp.prompt(
        name="explore_nested_data",
        description="Guide UNNEST / OBJECT_NAMES traversal of nested documents.",
    )
    async def explore_nested_data(dataverse: str | None = None, dataset: str | None = None) -> str:
        return compose_explore_nested_data(dataverse, dataset)

    @mcp.prompt(
        name="explain_error",
        description="Translate an AsterixDB error into cause and fix.",
    )
    async def explain_error(error: str | None = None) -> str:
        return compose_explain_error(error)

    # Argument completion (prompts and resource templates)

    # Bind the decorator through the typed Protocol boundary (see
    # _SupportsCompletion) so the handler below stays a typed decorator target.
    completion_decorator = cast(_SupportsCompletion, mcp).completion()

    @completion_decorator
    async def complete(
        ref: types.PromptReference | types.ResourceTemplateReference,
        argument: types.CompletionArgument,
        context: types.CompletionContext | None,
    ) -> types.Completion | None:
        context_arguments = context.arguments if context is not None else None
        return await complete_argument(
            _client(),
            settings,
            argument_name=argument.name,
            partial=argument.value,
            context_arguments=context_arguments,
        )

    # Advertise each tool's successful-result shape (decoupled from validation).
    apply_output_schemas(mcp)

    return mcp


def main() -> None:
    """Console-script entry point: build the server and serve on the configured transport.

    stdio (the default) runs the local sidecar. http serves the Streamable HTTP
    transport — with a /health probe and optional bearer auth — via uvicorn.
    """
    settings = load_settings()
    server = build_server(settings)
    if settings.transport == "http":
        _serve_http(server, settings)
        return
    server.run()


def _serve_http(server: FastMCP, settings: Settings) -> None:
    """Serve the Streamable HTTP app over uvicorn (imported lazily for stdio runs)."""
    import uvicorn

    from .http_app import build_http_app

    app = build_http_app(server, settings)
    uvicorn.run(app, host=settings.http_host, port=settings.http_port)
