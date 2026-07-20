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
    # The configured id is the session prefix; a per-process suffix follows.
    assert settings.agent_session_id.startswith("sess-xyz-")


def test_defaults_match_constants() -> None:
    settings = Settings()
    assert settings.max_time_ms == DEFAULT_MAX_TIME_MS
    assert settings.max_bytes_per_query == DEFAULT_MAX_BYTES_PER_QUERY
    assert settings.distill_interval_s == 0  # auto-distill off unless opted in
    assert settings.auto_maintenance_enabled is True  # startup maintenance on by default


def test_load_settings_makes_session_id_process_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The configured id is a prefix; each load (one per gateway process) gets a
    # distinct suffix so cross-session distillation sees distinct sessions.
    monkeypatch.setenv("ASTERIXDB_MCP_AGENT_SESSION_ID", "antigravity-session")
    first = load_settings()
    second = load_settings()
    assert first.agent_session_id.startswith("antigravity-session-")
    assert second.agent_session_id.startswith("antigravity-session-")
    assert first.agent_session_id != second.agent_session_id
