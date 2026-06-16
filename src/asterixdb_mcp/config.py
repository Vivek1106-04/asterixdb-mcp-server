"""Gateway configuration.

All settings are loaded from environment variables (prefix ``ASTERIXDB_MCP_``)
so the gateway can be configured purely through the deployment environment with
no config file on disk. Defaults target a local single-node AsterixDB cluster.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default egress ceilings. Documented as constants so the rationale lives next
# to the number instead of being buried in a magic literal.
DEFAULT_MAX_TIME_MS = 30_000  # synchronous query wall-clock ceiling (CC `timeout`)
DEFAULT_MAX_BYTES_PER_QUERY = 10 * 1024 * 1024  # 10 MiB gateway-side response cap
DEFAULT_REQUEST_TIMEOUT_S = 35.0  # httpx transport timeout; must exceed max_time_ms

# Concurrency permit pool sizes (see permits.py).
DEFAULT_SYNC_PERMITS = 3  # blocking execute_query calls in flight
DEFAULT_ASYNC_PERMITS = 2  # async submissions in flight
DEFAULT_MAX_CONCURRENT_WAITS = 16  # in-gateway long-poll loops in flight

# Async long-poll bounds (see wait_on_async_query).
DEFAULT_MAX_WAIT_MS = 10_000  # ceiling on a single wait_on_async_query call
DEFAULT_WAIT_POLL_INTERVAL_MS = 250  # cadence of the internal status poll
DEFAULT_AUDIT_LOG_TTL_S = 900.0  # how long a submission stays in the audit log

# Egress layer 4: ceilings on what actually reaches the LLM (context-window guard).
DEFAULT_MAX_ROWS_TO_LLM = 200  # rows delivered before truncation metadata kicks in
DEFAULT_MAX_BYTES_TO_LLM = 256 * 1024  # serialized result bytes delivered to the LLM
DEFAULT_MAX_FIELD_CHARS = 500  # per-string-value clamp so one huge field can't dominate

# Transport. stdio is the default (a local sidecar spoken to over stdin/stdout);
# http exposes the Streamable HTTP transport for remote / multi-client access.
DEFAULT_TRANSPORT: Literal["stdio", "http"] = "stdio"
# Default HTTP port 19200: same 19xxx family as the AsterixDB Cluster Controller
# (19002) but clear of the range the cluster itself binds (19001-19098), so the
# gateway does not collide with a co-located AsterixDB or with common dev servers
# on 8000/8080. Loopback host by default; bind 0.0.0.0 only behind a reverse proxy.
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 19200
DEFAULT_HTTP_PATH = "/mcp"  # Streamable HTTP endpoint path
HEALTH_PATH = "/health"  # unauthenticated liveness probe path

# Authentication. none = open (allowed on loopback only); bearer = shared static
# token; oauth = OAuth 2.1 resource-server JWT validation against an external AS.
DEFAULT_AUTH_MODE: Literal["none", "bearer", "oauth"] = "none"
MIN_API_KEY_LENGTH = 16  # reject trivially-guessable bearer tokens at startup
DEFAULT_OAUTH_ALGORITHMS = ["RS256"]  # asymmetric default; 'none' always rejected, HS* opt-in only


class Settings(BaseSettings):
    """Runtime configuration for the AsterixDB MCP gateway."""

    model_config = SettingsConfigDict(
        env_prefix="ASTERIXDB_MCP_",
        env_file=".env",
        extra="ignore",
    )

    # AsterixDB Cluster Controller connection
    cc_base_url: str = Field(
        default="http://localhost:19002",
        description="Base URL of the AsterixDB Cluster Controller REST API.",
    )
    cc_shared_secret: str | None = Field(
        default=None,
        description="Optional shared secret sent as the X-Gateway-Secret header on the "
        "gateway-to-CC hop. Local development leaves this unset.",
    )

    # Session identity
    agent_session_id: str = Field(
        default="local-session",
        description="Stable identifier for this gateway instance, used as the leading "
        "segment of the namespaced clientContextID ({agentSessionId}::{userTag}::{uuid}).",
    )

    # Egress ceilings (4-layer guardrails)
    max_time_ms: int = Field(
        default=DEFAULT_MAX_TIME_MS,
        ge=1,
        description="Egress layer 1: per-query wall-clock ceiling forwarded to the CC "
        "as the `timeout` duration parameter.",
    )
    max_bytes_per_query: int = Field(
        default=DEFAULT_MAX_BYTES_PER_QUERY,
        ge=1,
        description="Egress layer 2: maximum response size the gateway will buffer from "
        "the CC. Enforced gateway-side; not a native CC parameter.",
    )
    request_timeout_s: float = Field(
        default=DEFAULT_REQUEST_TIMEOUT_S,
        gt=0,
        description="httpx transport timeout for the gateway-to-CC hop.",
    )

    # Concurrency permits (backpressure, not queueing)
    sync_permits: int = Field(
        default=DEFAULT_SYNC_PERMITS,
        ge=1,
        description="Max concurrent synchronous execute_query calls before NOT_READY.",
    )
    async_permits: int = Field(
        default=DEFAULT_ASYNC_PERMITS,
        ge=1,
        description="Max concurrent async query submissions before NOT_READY.",
    )
    max_concurrent_waits: int = Field(
        default=DEFAULT_MAX_CONCURRENT_WAITS,
        ge=1,
        description="Max concurrent wait_on_async_query long-poll loops before NOT_READY.",
    )

    # Async long-poll behavior
    max_wait_ms: int = Field(
        default=DEFAULT_MAX_WAIT_MS,
        ge=1,
        description="Ceiling on a single wait_on_async_query timeoutMs. Requests above "
        "this are clamped down.",
    )
    wait_poll_interval_ms: int = Field(
        default=DEFAULT_WAIT_POLL_INTERVAL_MS,
        ge=1,
        description="Cadence of the internal status poll inside wait_on_async_query.",
    )
    audit_log_ttl_s: float = Field(
        default=DEFAULT_AUDIT_LOG_TTL_S,
        gt=0,
        description="How long a submission's metadata is retained in the audit log.",
    )

    # Egress layer 4 (rows/bytes delivered to the LLM)
    max_rows_to_llm: int = Field(
        default=DEFAULT_MAX_ROWS_TO_LLM,
        ge=1,
        description="Max result rows delivered to the LLM before truncation metadata.",
    )
    max_bytes_to_llm: int = Field(
        default=DEFAULT_MAX_BYTES_TO_LLM,
        ge=1,
        description="Max serialized result bytes delivered to the LLM before truncation.",
    )
    max_field_chars: int = Field(
        default=DEFAULT_MAX_FIELD_CHARS,
        ge=1,
        description="Max characters of any single string field value before it is clamped.",
    )

    # Transport selection and HTTP server binding
    transport: Literal["stdio", "http"] = Field(
        default=DEFAULT_TRANSPORT,
        description="Transport to serve on: 'stdio' (local sidecar) or 'http' "
        "(Streamable HTTP for remote/multi-client access).",
    )
    http_host: str = Field(
        default=DEFAULT_HTTP_HOST,
        description="Host interface to bind when transport='http'. Defaults to loopback; "
        "set 0.0.0.0 only behind a trusted reverse proxy.",
    )
    http_port: int = Field(
        default=DEFAULT_HTTP_PORT,
        ge=1,
        le=65535,
        description="TCP port to bind when transport='http'.",
    )
    http_path: str = Field(
        default=DEFAULT_HTTP_PATH,
        description="URL path of the Streamable HTTP MCP endpoint.",
    )
    # DNS-rebinding protection: the gateway's own host:port plus loopback are
    # always allowed; these extend the allowlist for a proxied public deployment
    # (e.g. the proxy's Host and the browser Origin it forwards).
    http_allowed_hosts: list[str] = Field(
        default_factory=list,
        description="Extra Host header values to allow (e.g. a reverse-proxy host).",
    )
    http_allowed_origins: list[str] = Field(
        default_factory=list,
        description="Extra Origin header values to allow (e.g. https://app.example.com).",
    )

    # Authentication boundary for the HTTP transport
    auth_mode: Literal["none", "bearer", "oauth"] = Field(
        default=DEFAULT_AUTH_MODE,
        description="HTTP auth: 'none' (loopback-only), 'bearer' (static token), or "
        "'oauth' (OAuth 2.1 resource-server JWT validation).",
    )
    api_key: str | None = Field(
        default=None,
        description="Bearer token required on every HTTP request except the health probe "
        "when auth_mode='bearer'. Must be at least "
        f"{MIN_API_KEY_LENGTH} characters.",
    )

    # OAuth 2.1 resource-server settings (auth_mode='oauth'). The gateway verifies
    # tokens issued by an external authorization server; it never issues them.
    oauth_issuer: str = Field(
        default="",
        description="Issuer URL of the authorization server (the JWT `iss` claim).",
    )
    oauth_audience: str = Field(
        default="",
        description="This resource server's audience: the gateway's canonical URL, "
        "matched against the JWT `aud` claim (RFC 8707).",
    )
    oauth_jwks_uri: str = Field(
        default="",
        description="JWKS endpoint of the authorization server, used to verify token signatures.",
    )
    oauth_required_scopes: list[str] = Field(
        default_factory=list,
        description="Scopes a token must carry to access the gateway (all required).",
    )
    oauth_algorithms: list[str] = Field(
        default_factory=lambda: list(DEFAULT_OAUTH_ALGORITHMS),
        description="Accepted JWT signing algorithms. Defaults to RS256.",
    )


def load_settings() -> Settings:
    """Load settings from the environment. Kept as a function so tests can call it
    with a patched environment and the server has a single construction point."""
    return Settings()
