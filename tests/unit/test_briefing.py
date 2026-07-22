"""Unit tests for the session-start briefing."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.tools import ToolResult
from asterixdb_mcp.tools.briefing import (
    BriefingState,
    build_briefing,
    maybe_attach_briefing,
    render_briefing,
)

pytestmark = pytest.mark.anyio


def _dataset(dv: str, ds: str, columnar: bool = False) -> dict:
    row = {"DataverseName": dv, "DatasetName": ds, "DatasetType": "INTERNAL"}
    if columnar:
        row["DatasetFormat"] = {"Format": "column"}
    return row


def _branching_cc(settings: Settings, dataset_rows: list[dict], pref_rows: list[dict]):
    """CC whose response depends on which store the statement targets."""
    from tests.conftest import make_capturing_cc

    def handler(req: httpx.Request) -> httpx.Response:
        statement = req.content.decode()
        rows = pref_rows if "AgentMemory.Memory" in statement else dataset_rows
        return httpx.Response(200, json={"status": "success", "results": rows})

    return make_capturing_cc(settings, handler=handler)


# render_briefing (pure)


def test_render_lists_dataverses_datasets_columnar_and_rules() -> None:
    rows = [
        _dataset("real_estate", "listings", columnar=True),
        _dataset("real_estate", "agents"),
        _dataset("Metadata", "Dataset"),  # system rows excluded
    ]
    text = render_briefing(rows, preferences=[])
    assert "Dataverses (1): real_estate" in text
    assert "Datasets: 2 (1 COLUMNAR" in text
    assert "project the columns you need" in text
    assert "Per-dataset schema" in text
    assert "Preferences:" not in text  # none given


def test_render_includes_preferences_when_present() -> None:
    rows = [_dataset("dv", "d1")]
    text = render_briefing(rows, preferences=["quote reserved words", "prefer CA"])
    assert "Preferences: quote reserved words; prefer CA" in text
    assert "COLUMNAR — project" not in text  # no columnar dataset -> no count suffix


def test_render_empty_when_only_system_datasets() -> None:
    assert render_briefing([_dataset("Metadata", "Dataset")], preferences=[]) == ""
    assert render_briefing([], preferences=[]) == ""


def test_render_caps_dataverse_list() -> None:
    rows = [_dataset(f"dv{i:02d}", "d") for i in range(20)]
    text = render_briefing(rows, preferences=[])
    assert "+8 more" in text  # 20 dataverses, 12 shown


# BriefingState


def test_briefing_state_is_one_shot() -> None:
    state = BriefingState()
    assert state.pending() is True
    state.mark()
    assert state.pending() is False


# build_briefing


async def test_build_briefing_assembles_from_stores(settings: Settings) -> None:
    cap = _branching_cc(
        settings,
        dataset_rows=[_dataset("real_estate", "listings", columnar=True)],
        pref_rows=[{"subject": "_pref/global", "type": "Preference", "text": "project columns"}],
    )
    text = await build_briefing(cap.client, "ccid")
    assert "real_estate" in text
    assert "Preferences: project columns" in text


async def test_build_briefing_empty_on_inventory_error(settings: Settings) -> None:
    from tests.conftest import make_capturing_cc

    body = {"status": "fatal", "errors": [{"code": "ASX1050", "msg": "down"}]}
    cap = make_capturing_cc(settings, response_json=body)
    assert await build_briefing(cap.client, "ccid") == ""


# maybe_attach_briefing


async def test_attach_prepends_briefing_once(settings: Settings) -> None:
    cap = _branching_cc(
        settings,
        dataset_rows=[_dataset("dv", "d1")],
        pref_rows=[],
    )
    state = BriefingState()
    base = ToolResult(text="TOOL OUTPUT", structured={"status": "success"})

    first = await maybe_attach_briefing(cap.client, settings, state, base)
    assert first.text.startswith("Session briefing")
    assert first.text.endswith("TOOL OUTPUT")
    assert first.structured == {"status": "success"}
    assert state.pending() is False

    second = await maybe_attach_briefing(cap.client, settings, state, base)
    assert second is base  # already delivered


async def test_attach_stays_pending_when_briefing_empty(settings: Settings) -> None:
    # No user datasets yet -> nothing to say -> flag not spent, retries later.
    cap = _branching_cc(settings, dataset_rows=[_dataset("Metadata", "Dataset")], pref_rows=[])
    state = BriefingState()
    base = ToolResult(text="OUT", structured=None)

    result = await maybe_attach_briefing(cap.client, settings, state, base)
    assert result is base
    assert state.pending() is True
