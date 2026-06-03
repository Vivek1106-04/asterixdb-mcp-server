"""Unit tests for settings loading."""

from __future__ import annotations

import pytest

from asterixdb_mcp.config import (
    DEFAULT_MAX_BYTES_PER_QUERY,
    DEFAULT_MAX_TIME_MS,
    Settings,
    load_settings,
)


def test_load_settings_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTERIXDB_MCP_CC_BASE_URL", "http://cc.example:19002")
    monkeypatch.setenv("ASTERIXDB_MCP_AGENT_SESSION_ID", "sess-xyz")
    settings = load_settings()
    assert settings.cc_base_url == "http://cc.example:19002"
    assert settings.agent_session_id == "sess-xyz"


def test_defaults_match_constants() -> None:
    settings = Settings()
    assert settings.max_time_ms == DEFAULT_MAX_TIME_MS
    assert settings.max_bytes_per_query == DEFAULT_MAX_BYTES_PER_QUERY
