"""Gateway configuration.

All settings are loaded from environment variables (prefix ``ASTERIXDB_MCP_``)
so the gateway can be configured purely through the deployment environment with
no config file on disk. Defaults target a local single-node AsterixDB cluster.
"""

from __future__ import annotations

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


def load_settings() -> Settings:
    """Load settings from the environment. Kept as a function so tests can call it
    with a patched environment and the server has a single construction point."""
    return Settings()
