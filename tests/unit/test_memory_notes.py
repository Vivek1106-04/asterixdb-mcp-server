"""Unit tests for server-side auto-recall of learned memory notes."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.tools import ToolResult
from asterixdb_mcp.tools.memory_notes import (
    MAX_NOTE_LEN,
    MAX_NOTE_SUBJECTS,
    RecallState,
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

    # Both notes are delivered; order is score-driven, so compare by subject.
    by_subject = {n["subject"]: n for n in notes}
    assert by_subject["ShopDV.customers"] == {
        "subject": "ShopDV.customers",
        "note": "the tags field is a CSV string",
        "grounded": False,
    }
    assert by_subject["ShopDV"] == {"subject": "ShopDV", "note": "learned", "grounded": None}
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
    assert result.structured["errorType"] == "x"
    assert result.structured["learnedNotes"][0]["note"] == "use split() on tags"
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


def test_recall_state_fresh_filters_marked_subjects() -> None:
    state = RecallState()
    assert state.fresh(["ShopDV.orders"]) == ["ShopDV.orders"]
    state.mark(["ShopDV.orders"])
    assert state.fresh(["ShopDV.orders", "HR.employees"]) == ["HR.employees"]


async def test_recall_marks_only_subjects_whose_notes_were_delivered(
    settings: Settings,
) -> None:
    # First query: the dataset has no notes yet, so the subject must stay
    # fresh; a note written later in the session still surfaces on the next
    # query, and only then is the subject marked as delivered.
    note_row = {"subject": "ShopDV.orders", "type": "Note", "text": "amount is a string"}
    responses = iter([[], [note_row], [note_row]])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "results": next(responses)})

    cap = make_capturing_cc(settings, handler=handler)
    base = ToolResult(text="ok", structured={"status": "success"})
    state = RecallState()
    stmt = "SELECT * FROM ShopDV.orders o;"

    first = await attach_statement_notes(
        cap.client, "ccid", stmt, base, recall=state, first_use_only=True
    )
    assert first is base  # no notes delivered -> not marked

    second = await attach_statement_notes(
        cap.client, "ccid", stmt, base, recall=state, first_use_only=True
    )
    assert "amount is a string" in second.text  # later-written note surfaces

    third = await attach_statement_notes(
        cap.client, "ccid", stmt, base, recall=state, first_use_only=True
    )
    assert third is base  # delivered once -> deduped for the session


async def test_error_path_attaches_every_time_but_marks_delivery(settings: Settings) -> None:
    note_row = {"subject": "HR.employees", "type": "Note", "text": "salary is in cents"}
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": [note_row]})
    base = ToolResult(text="failed", structured={}, is_error=True)
    state = RecallState()
    stmt = "SELECT * FROM HR.employees e;"

    attached = await attach_statement_notes(
        cap.client, "ccid", stmt, base, recall=state, first_use_only=False
    )
    assert "salary is in cents" in attached.text

    ok = ToolResult(text="ok", structured={"status": "success"})
    deduped = await attach_statement_notes(
        cap.client, "ccid", stmt, ok, recall=state, first_use_only=True
    )
    assert deduped is ok  # the failure already delivered these notes


def test_render_notes_labels_evidence_status() -> None:
    rendered = render_notes(
        [
            {"subject": "a.b", "note": "proven", "grounded": True},
            {"subject": "a.c", "note": "hearsay", "grounded": False},
            {"subject": "a", "note": "overlay text", "grounded": None},
        ]
    )
    assert "- [a.b] (grounded) proven" in rendered
    assert "- [a.c] (unverified) hearsay" in rendered
    assert "- [a] overlay text" in rendered


async def test_grounded_flag_reflects_source_query(settings: Settings) -> None:
    rows = [
        {"subject": "HR.employees", "type": "Note", "text": "fact", "source_query": "SELECT 1;"}
    ]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    notes = await fetch_memory_notes(cap.client, "ccid", ["HR.employees"])
    assert notes[0]["grounded"] is True


async def test_attach_with_recall_skips_already_delivered_subjects(settings: Settings) -> None:
    rows = [{"subject": "ShopDV.customers", "type": "Note", "text": "note"}]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    state = RecallState()
    base = ToolResult(text="ok", structured={"status": "success"})

    first = await attach_statement_notes(
        cap.client,
        "ccid",
        "SELECT * FROM ShopDV.customers;",
        base,
        recall=state,
        first_use_only=True,
    )
    assert "note" in first.text and first.structured["learnedNotes"]

    second = await attach_statement_notes(
        cap.client,
        "ccid",
        "SELECT * FROM ShopDV.customers;",
        base,
        recall=state,
        first_use_only=True,
    )
    assert second.text == "ok"
    assert len(cap.requests) == 1  # no second lookup for a claimed subject


async def test_attach_leaves_missing_structured_alone(settings: Settings) -> None:
    rows = [{"subject": "ShopDV.customers", "type": "Note", "text": "note"}]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    base = ToolResult(text="ok", structured=None)
    result = await attach_statement_notes(
        cap.client, "ccid", "SELECT * FROM ShopDV.customers;", base
    )
    assert result.structured is None
    assert "note" in result.text


# note_score + ranking


def test_note_score_prefers_verified_recalled_recent() -> None:
    from datetime import datetime, timezone

    from asterixdb_mcp.tools.memory_notes import note_score

    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    grounded = {"type": "Note", "source_query": "SELECT 1;", "valid_from": now.isoformat()}
    bare = {"type": "Note", "valid_from": now.isoformat()}
    assert note_score(grounded, now) > note_score(bare, now)

    used = {"type": "Note", "recall_count": 20, "valid_from": now.isoformat()}
    assert note_score(used, now) > note_score(bare, now)


def test_note_score_freshness_decays_with_age() -> None:
    from datetime import datetime, timedelta, timezone

    from asterixdb_mcp.tools.memory_notes import note_score

    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    fresh = {"type": "Note", "valid_from": now.isoformat()}
    old = {"type": "Note", "valid_from": (now - timedelta(days=365)).isoformat()}
    assert note_score(fresh, now) > note_score(old, now)


def test_note_score_undated_row_ranks_as_old() -> None:
    from datetime import datetime, timezone

    from asterixdb_mcp.tools.memory_notes import note_score

    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    undated = {"type": "Note"}
    fresh = {"type": "Note", "valid_from": now.isoformat()}
    assert note_score(undated, now) < note_score(fresh, now)


async def test_fetch_note_rows_caps_and_ranks(settings: Settings) -> None:
    from asterixdb_mcp.tools.memory_notes import MAX_ATTACHED_NOTES, fetch_note_rows

    rows = [
        {"subject": "D.s", "type": "Note", "text": f"note {i}", "recall_count": i}
        for i in range(MAX_ATTACHED_NOTES + 3)
    ]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    got = await fetch_note_rows(cap.client, "ccid", ["D.s"])
    assert len(got) == MAX_ATTACHED_NOTES
    # Highest recall_count ranks first.
    assert got[0]["recall_count"] == MAX_ATTACHED_NOTES + 2


# one-hop link expansion


def test_link_subjects_filters_seen_invalid_and_dedupes() -> None:
    from asterixdb_mcp.tools.memory_notes import _link_subjects

    rows = [
        {"links": ["A.b", "C.d", "A.b"]},  # A.b is seen; dup C.d collapses
        {"links": ["E.f", "bad subject!"]},  # invalid identifier rejected
        {"links": None},  # missing links tolerated
    ]
    assert _link_subjects(rows, seen={"A.b"}) == ["C.d", "E.f"]


def test_link_subjects_caps_the_follow() -> None:
    from asterixdb_mcp.tools.memory_notes import MAX_NOTE_SUBJECTS, _link_subjects

    rows = [{"links": [f"D.s{i}" for i in range(MAX_NOTE_SUBJECTS + 3)]}]
    assert len(_link_subjects(rows, seen=set())) == MAX_NOTE_SUBJECTS


def test_dedupe_rows_keeps_first_by_id() -> None:
    from asterixdb_mcp.tools.memory_notes import _dedupe_rows

    rows = [{"id": "1", "n": "a"}, {"id": "2", "n": "b"}, {"id": "1", "n": "dup"}]
    assert _dedupe_rows(rows) == [{"id": "1", "n": "a"}, {"id": "2", "n": "b"}]


async def test_fetch_note_rows_follows_links_one_hop(settings: Settings) -> None:
    from asterixdb_mcp.tools.memory_notes import fetch_note_rows

    direct = [
        {
            "id": "D.s@t",
            "subject": "D.s",
            "type": "Note",
            "text": "main note",
            "links": ["D.s/index/city_idx"],
        }
    ]
    linked = [
        {
            "id": "D.s/index/city_idx@t",
            "subject": "D.s/index/city_idx",
            "type": "AsterixDB Index",
            "text": "core",
            "overlay": "use this index for city lookups",
        }
    ]
    responses = iter([direct, linked])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "results": next(responses)})

    cap = make_capturing_cc(settings, handler=handler)
    got = await fetch_note_rows(cap.client, "ccid", ["D.s"])

    subjects = {row["subject"] for row in got}
    assert subjects == {"D.s", "D.s/index/city_idx"}
    # The second query fetched exactly the linked subject.
    assert parse_qs(cap.requests[1].content.decode())["$subjects"][0] == '["D.s/index/city_idx"]'


async def test_fetch_note_rows_no_links_issues_single_query(settings: Settings) -> None:
    from asterixdb_mcp.tools.memory_notes import fetch_note_rows

    rows = [{"id": "D.s@t", "subject": "D.s", "type": "Note", "text": "no links here"}]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    got = await fetch_note_rows(cap.client, "ccid", ["D.s"])
    assert len(got) == 1
    assert len(cap.requests) == 1  # no second hop when nothing links out


# reinforcement on delivery


async def test_delivery_reinforces_when_writes_enabled(settings: Settings) -> None:
    import json

    settings = settings.model_copy(update={"memory_write_enabled": True})
    rows = [{"id": "D.s@t0", "subject": "D.s", "type": "Note", "text": "note"}]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    base = ToolResult(text="ok", structured={"status": "success"})

    await attach_statement_notes(cap.client, "ccid", "SELECT * FROM D.s;", base, settings=settings)

    upserts = [
        parse_qs(r.content.decode())
        for r in cap.requests
        if "UPSERT" in parse_qs(r.content.decode())["statement"][0]
    ]
    assert len(upserts) == 1
    bumped = json.loads(upserts[0]["$row"][0])
    assert bumped["id"] == "D.s@t0"  # same row, not a new bi-temporal version
    assert bumped["recall_count"] == 1
    assert bumped["last_recalled_at"]


async def test_no_reinforcement_when_writes_disabled(settings: Settings) -> None:
    rows = [{"id": "D.s@t0", "subject": "D.s", "type": "Note", "text": "note"}]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    base = ToolResult(text="ok", structured={"status": "success"})

    await attach_statement_notes(cap.client, "ccid", "SELECT * FROM D.s;", base, settings=settings)

    statements = [parse_qs(r.content.decode())["statement"][0] for r in cap.requests]
    assert not any("UPSERT" in s for s in statements)  # read-only: no bump


def test_note_score_naive_last_recalled_treated_as_utc() -> None:
    from datetime import datetime, timezone

    from asterixdb_mcp.tools.memory_notes import note_score

    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    naive = {"type": "Note", "last_recalled_at": "2026-07-20T00:00:00"}  # no offset
    aware = {"type": "Note", "last_recalled_at": "2026-07-20T00:00:00+00:00"}
    assert note_score(naive, now) == note_score(aware, now)


async def test_reinforce_degrades_on_store_error(settings: Settings) -> None:
    from asterixdb_mcp.errors import ErrorType, GatewayError
    from asterixdb_mcp.tools.memory_notes import reinforce_notes

    settings = settings.model_copy(update={"memory_write_enabled": True})

    def boom(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "UPSERT" in request.content.decode():
            return httpx.Response(200, json={"status": "fatal", "errors": [{"msg": "down"}]})
        return httpx.Response(200, json={"status": "success", "results": []})

    cap = make_capturing_cc(settings, handler=boom)
    rows = [{"id": "D.s@t0", "subject": "D.s", "type": "Note", "text": "note"}]
    # Must not raise: reinforcement is best-effort metadata.
    await reinforce_notes(cap.client, "ccid", rows)
    _ = (ErrorType, GatewayError)  # imported to document the swallowed type
