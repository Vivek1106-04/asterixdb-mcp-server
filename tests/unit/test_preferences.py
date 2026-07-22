"""Unit tests for the remember_preference tool and preference retrieval."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.tools.preferences import (
    GLOBAL_SCOPE,
    MAX_PREF_LEN,
    PREFERENCE_KIND,
    PREFERENCE_TYPE,
    fetch_active_preferences,
    preference_subject,
    run_remember_preference,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _writable(settings: Settings) -> Settings:
    return settings.model_copy(update={"memory_write_enabled": True})


def _insert_row(cap) -> dict:
    for req in cap.requests:
        form = parse_qs(req.content.decode())
        if "INSERT" in form["statement"][0]:
            return json.loads(form["$row"][0])
    raise AssertionError("no INSERT statement was issued")


# guards


async def test_write_disabled_is_forbidden(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_remember_preference(cap.client, settings, text="rule")
    assert result.is_error is True
    assert result.structured["errorType"] == "FORBIDDEN"
    assert cap.requests == []


async def test_empty_text_rejected(settings: Settings) -> None:
    settings = _writable(settings)
    cap = make_capturing_cc(settings)
    result = await run_remember_preference(cap.client, settings, text="   ")
    assert result.is_error is True
    assert result.structured["errorType"] == "INVALID_PARAMETER"


async def test_overlong_text_rejected(settings: Settings) -> None:
    settings = _writable(settings)
    cap = make_capturing_cc(settings)
    result = await run_remember_preference(
        cap.client, settings, text="x" * (MAX_PREF_LEN + 1)
    )
    assert result.is_error is True
    assert result.structured["errorType"] == "INVALID_PARAMETER"


async def test_invalid_scope_rejected(settings: Settings) -> None:
    settings = _writable(settings)
    cap = make_capturing_cc(settings)
    result = await run_remember_preference(
        cap.client, settings, text="rule", scope="bad scope!"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == "INVALID_PARAMETER"


# write


async def test_create_writes_preference_row(settings: Settings) -> None:
    settings = _writable(settings)
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_remember_preference(
        cap.client, settings, text="project columns", scope="SalesDV", author="claude/1"
    )
    assert result.structured["action"] == "created"
    row = _insert_row(cap)
    assert row["subject"] == preference_subject("SalesDV") == "_pref/SalesDV"
    assert row["type"] == PREFERENCE_TYPE
    assert row["kind"] == PREFERENCE_KIND
    assert row["scope"] == "SalesDV"
    assert row["text"] == "project columns"
    assert row["author"] == "claude/1"
    assert "valid_to" not in row  # current row


async def test_blank_scope_defaults_to_global(settings: Settings) -> None:
    settings = _writable(settings)
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_remember_preference(cap.client, settings, text="rule", scope="   ")
    assert result.structured["scope"] == GLOBAL_SCOPE
    assert _insert_row(cap)["subject"] == preference_subject(GLOBAL_SCOPE)


async def test_duplicate_preference_is_noop(settings: Settings) -> None:
    settings = _writable(settings)
    existing = [{"subject": "_pref/global", "type": "Preference", "text": "quote reserved words"}]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": existing})
    result = await run_remember_preference(
        cap.client, settings, text="quote reserved words"
    )
    assert result.structured["action"] == "unchanged"
    assert result.structured["id"] is None
    statements = [parse_qs(r.content.decode())["statement"][0] for r in cap.requests]
    assert not any("INSERT" in s for s in statements)  # nothing written


async def test_store_error_on_write_returns_error(settings: Settings) -> None:
    settings = _writable(settings)
    body = {"status": "fatal", "errors": [{"code": "ASX1050", "msg": "down"}]}
    cap = make_capturing_cc(settings, response_json=body)
    result = await run_remember_preference(cap.client, settings, text="rule")
    assert result.is_error is True


# fetch_active_preferences


async def test_fetch_active_returns_and_dedupes_texts(settings: Settings) -> None:
    rows = [
        {"subject": "_pref/global", "type": "Preference", "text": "rule A"},
        {"subject": "_pref/global", "type": "Preference", "text": "rule B"},
        {"subject": "_pref/global", "type": "Preference", "text": "rule A"},  # dup
        "junk",
        {"subject": "_pref/global", "type": "Preference", "text": "  "},  # blank skipped
    ]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    prefs = await fetch_active_preferences(cap.client, "ccid", [GLOBAL_SCOPE])
    assert prefs == ["rule A", "rule B"]
    form = parse_qs(cap.requests[0].content.decode())
    assert form["$subjects"][0] == '["_pref/global"]'


async def test_fetch_active_no_valid_scopes_issues_no_query(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    assert await fetch_active_preferences(cap.client, "ccid", ["bad scope!"]) == []
    assert cap.requests == []


async def test_fetch_active_swallows_store_errors(settings: Settings) -> None:
    body = {"status": "fatal", "errors": [{"code": "ASX1050", "msg": "down"}]}
    cap = make_capturing_cc(settings, response_json=body)
    assert await fetch_active_preferences(cap.client, "ccid", [GLOBAL_SCOPE]) == []
