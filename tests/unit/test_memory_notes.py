"""Unit tests for server-side auto-recall of learned memory notes."""

from __future__ import annotations

from urllib.parse import parse_qs

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.tools import ToolResult
from asterixdb_mcp.tools.memory_notes import (
    MAX_NOTE_LEN,
    MAX_NOTE_SUBJECTS,
    attach_statement_notes,
    fetch_memory_notes,
    render_notes,
    subjects_from_statement,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


# subjects_from_statement


def test_subjects_extracted_from_from_join_and_unnest() -> None:
    stmt = (
        "SELECT r.total FROM SalesDV.orders r JOIN ShopDV.customers b "
        "ON r.customer_id = b.customer_id UNNEST HR.employees o WHERE r.total > b.total;"
    )
    assert subjects_from_statement(stmt) == ["SalesDV.orders", "ShopDV.customers", "HR.employees"]


def test_subjects_skip_metadata_alias_paths_and_duplicates() -> None:
    stmt = (
        "SELECT d.DatasetName FROM Metadata.`Dataset` d "
        "WHERE EXISTS (SELECT 1 FROM ShopDV.customers x) "
        "AND EXISTS (SELECT 1 FROM ShopDV.customers y) "
        "AND EXISTS (SELECT 1 FROM Sales.orders o);"
    )
    assert subjects_from_statement(stmt) == ["ShopDV.customers", "Sales.orders"]


def test_subjects_handle_backticked_names() -> None:
    assert subjects_from_statement("SELECT * FROM `ShopDV`.`Checkin` c;") == ["ShopDV.Checkin"]


# fetch_memory_notes


async def test_fetch_returns_learned_notes_and_binds_subjects(settings: Settings) -> None:
    rows = [
        {"subject": "ShopDV.customers", "type": "Note", "text": "the tags field is a CSV string"},
        {"subject": "ShopDV", "type": "AsterixDB Dataverse", "text": "core", "overlay": "learned"},
    ]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})

    notes = await fetch_memory_notes(cap.client, "ccid", ["ShopDV.customers", "ShopDV"])

    assert notes == [
        {"subject": "ShopDV.customers", "note": "the tags field is a CSV string"},
        {"subject": "ShopDV", "note": "learned"},
    ]
    form = parse_qs(cap.requests[0].content.decode())
    assert form["$subjects"][0] == '["ShopDV.customers", "ShopDV"]'


async def test_fetch_filters_invalid_subjects_and_caps_the_list(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    subjects = ["bad subject!", "", "A.B", "A.B", "C.D", "E.F", "G.H", "I.J"]

    await fetch_memory_notes(cap.client, "ccid", subjects)

    form = parse_qs(cap.requests[0].content.decode())
    bound = form["$subjects"][0]
    assert "bad subject!" not in bound
    assert bound.count(".") == MAX_NOTE_SUBJECTS


async def test_fetch_with_no_valid_subjects_issues_no_query(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    assert await fetch_memory_notes(cap.client, "ccid", ["not valid!"]) == []
    assert cap.requests == []


async def test_fetch_swallows_store_errors(settings: Settings) -> None:
    body = {"status": "fatal", "errors": [{"code": "ASX1050", "msg": "Cannot find dataset"}]}
    cap = make_capturing_cc(settings, response_json=body)
    assert await fetch_memory_notes(cap.client, "ccid", ["ShopDV.customers"]) == []


async def test_fetch_skips_non_dict_rows_and_empty_bodies(settings: Settings) -> None:
    rows = ["junk", {"subject": "A.B", "type": "Note", "text": "   "}, {"subject": "C.D"}]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    assert await fetch_memory_notes(cap.client, "ccid", ["A.B"]) == []


async def test_fetch_truncates_long_notes(settings: Settings) -> None:
    rows = [{"subject": "A.B", "type": "Note", "text": "x" * (MAX_NOTE_LEN + 50)}]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    notes = await fetch_memory_notes(cap.client, "ccid", ["A.B"])
    assert len(notes[0]["note"]) == MAX_NOTE_LEN


async def test_walk_owned_concept_without_overlay_contributes_nothing(settings: Settings) -> None:
    rows = [{"subject": "ShopDV.customers", "type": "AsterixDB Dataset", "text": "core only"}]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    assert await fetch_memory_notes(cap.client, "ccid", ["ShopDV.customers"]) == []


# attach_statement_notes


async def test_attach_appends_notes_to_error_result(settings: Settings) -> None:
    rows = [{"subject": "ShopDV.customers", "type": "Note", "text": "use split() on tags"}]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    base = ToolResult(text="QUERY_ERROR: boom", structured={"errorType": "x"}, is_error=True)

    result = await attach_statement_notes(
        cap.client, "ccid", "SELECT * FROM ShopDV.customers b;", base
    )

    assert result.is_error is True
    assert result.structured == base.structured
    assert "use split() on tags" in result.text
    assert result.text.startswith("QUERY_ERROR: boom")


async def test_attach_returns_result_unchanged_when_no_notes(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    base = ToolResult(text="QUERY_ERROR: boom", structured={}, is_error=True)
    result = await attach_statement_notes(
        cap.client, "ccid", "SELECT * FROM ShopDV.customers b;", base
    )
    assert result is base


def test_render_notes_formats_subject_and_note() -> None:
    rendered = render_notes([{"subject": "A.B", "note": "n1"}, {"subject": "C.D", "note": "n2"}])
    assert rendered == "- [A.B] n1\n- [C.D] n2"
