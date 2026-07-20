"""Unit tests for the decay pass (asterixdb_mcp.decay)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.decay import DECAY_AFTER_DAYS, is_decay_candidate, run_decay
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=DECAY_AFTER_DAYS + 5)).isoformat()
YOUNG = (NOW - timedelta(days=1)).isoformat()


def _note(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "ShopDV.orders@t0",
        "subject": "ShopDV.orders",
        "type": "Note",
        "kind": "semantic",
        "text": "unproven claim",
        "valid_from": OLD,
    }
    row.update(overrides)
    return row


# is_decay_candidate


def test_old_unverified_unrecalled_note_decays() -> None:
    assert is_decay_candidate(_note(), NOW) is True


def test_grounded_note_never_decays() -> None:
    assert is_decay_candidate(_note(source_query="SELECT 1;"), NOW) is False


def test_recalled_note_never_decays() -> None:
    assert is_decay_candidate(_note(recall_count=1), NOW) is False


def test_recent_delivery_resets_the_clock() -> None:
    assert is_decay_candidate(_note(last_recalled_at=YOUNG), NOW) is False


def test_young_note_does_not_decay() -> None:
    assert is_decay_candidate(_note(valid_from=YOUNG), NOW) is False


def test_walk_owned_concept_never_decays() -> None:
    assert is_decay_candidate(_note(type="AsterixDB Dataset"), NOW) is False


def test_malformed_timestamp_is_left_alone() -> None:
    assert is_decay_candidate(_note(valid_from="not-a-date"), NOW) is False
    assert is_decay_candidate(_note(valid_from=None), NOW) is False


def test_naive_timestamp_is_treated_as_utc() -> None:
    naive_old = (NOW - timedelta(days=DECAY_AFTER_DAYS + 5)).replace(tzinfo=None)
    assert is_decay_candidate(_note(valid_from=naive_old.isoformat()), NOW) is True


# run_decay


@pytest.fixture
def write_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"memory_write_enabled": True})


def _decay_handler(rows: list[dict[str, object]]):
    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        results = rows if stmt.startswith("SELECT") else []
        return httpx.Response(200, json={"status": "success", "results": results})

    return handler


async def test_run_decay_archives_only_candidates(write_settings: Settings) -> None:
    rows = [_note(), _note(id="kept@t0", source_query="SELECT 1;"), "junk"]
    cap = make_capturing_cc(write_settings, handler=_decay_handler(rows))

    summary = await run_decay(cap.client, write_settings)

    assert summary == {"candidates": 2, "archived": 1}
    upserts = [
        parse_qs(r.content.decode())
        for r in cap.requests
        if "UPSERT" in parse_qs(r.content.decode())["statement"][0]
    ]
    assert len(upserts) == 1
    archived = json.loads(upserts[0]["$row"][0])
    assert archived["id"] == "ShopDV.orders@t0"  # retired in place, same id
    assert archived["valid_to"]
    assert "never recalled" in archived["archived_reason"]


async def test_run_decay_with_nothing_to_archive_writes_nothing(
    write_settings: Settings,
) -> None:
    cap = make_capturing_cc(write_settings, handler=_decay_handler([_note(recall_count=3)]))
    summary = await run_decay(cap.client, write_settings)
    assert summary == {"candidates": 1, "archived": 0}
    statements = [parse_qs(r.content.decode())["statement"][0] for r in cap.requests]
    assert not any("UPSERT" in s for s in statements)
