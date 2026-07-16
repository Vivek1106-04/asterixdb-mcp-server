"""Unit tests for memory_write."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType, GatewayError
from asterixdb_mcp.tools.memory_write import MAX_LINKS, MAX_TEXT_LEN, run_memory_write
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


@pytest.fixture
def write_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"memory_write_enabled": True})


def _form_of(req: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(req.content.decode()).items()}


def _store_handler(existing: dict | None):
    """Answer the current-row lookup; acknowledge writes."""

    def handler(req: httpx.Request) -> httpx.Response:
        form = _form_of(req)
        if form["statement"].lstrip().startswith("SELECT"):
            rows = [existing] if existing else []
            return httpx.Response(200, json={"status": "success", "results": rows})
        return httpx.Response(200, json={"status": "success", "results": []})

    return handler


async def test_disabled_gateway_refuses_with_guidance(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_memory_write(cap.client, settings, subject="dv.ds", text="note")
    assert result.structured["errorType"] == ErrorType.FORBIDDEN.value
    assert "ASTERIXDB_MCP_MEMORY_WRITE_ENABLED" in result.structured["errorMessage"]
    assert cap.requests == []


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"subject": "dv.ds", "text": "   "}, "non-empty"),
        ({"subject": "dv.ds", "text": "x" * (MAX_TEXT_LEN + 1)}, "too long"),
        ({"subject": 'x"; DROP', "text": "note"}, "Invalid subject"),
        ({"subject": "dv.ds", "text": "note", "links": ["ok", "bad link"]}, "Invalid links"),
        ({"subject": "dv.ds", "text": "note", "links": ["l"] * (MAX_LINKS + 1)}, "Too many links"),
        ({"subject": "dv.ds", "text": "note", "replaces": "   "}, "non-empty replaces"),
    ],
)
async def test_invalid_input_rejected_preflight(
    write_settings: Settings, kwargs: dict, fragment: str
) -> None:
    cap = make_capturing_cc(write_settings)
    result = await run_memory_write(cap.client, write_settings, **kwargs)
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert fragment in result.structured["errorMessage"]
    assert cap.requests == []


async def test_new_subject_creates_note_concept(write_settings: Settings) -> None:
    cap = make_capturing_cc(write_settings, handler=_store_handler(None))
    result = await run_memory_write(
        cap.client,
        write_settings,
        subject="team/glossary",
        text="GMV means gross merchandise value",
        links=["shop.orders"],
        tags=["business"],
        source_query="SELECT 1;",
    )
    assert result.structured["action"] == "created"
    select_form, insert_form = (_form_of(req) for req in cap.requests)
    assert select_form["readonly"] == "true" and "$subject" in select_form
    assert insert_form["readonly"] == "false"
    assert insert_form["statement"].startswith("INSERT INTO AgentMemory.Memory")
    assert '"type": "Note"' in insert_form["$row"]
    assert '"trust": 1.0' in insert_form["$row"]
    assert '"source_query": "SELECT 1;"' in insert_form["$row"]


async def test_walk_owned_concept_gets_annotated_overlay(write_settings: Settings) -> None:
    existing = {
        "id": "shop.orders@t0",
        "subject": "shop.orders",
        "type": "AsterixDB Dataset",
        "core": "# Schema\n\n- `price`: int\n",
        "overlay": "- old note\n",
        "text": "# Schema\n\n- `price`: int\n\n- old note\n",
        "valid_from": "t0",
    }
    cap = make_capturing_cc(write_settings, handler=_store_handler(existing))
    result = await run_memory_write(
        cap.client, write_settings, subject="shop.orders", text="- `price` is in cents"
    )
    assert result.structured["action"] == "annotated"
    upsert_form, insert_form = (_form_of(req) for req in cap.requests[1:])
    assert upsert_form["statement"].startswith("UPSERT INTO AgentMemory.Memory")
    assert '"valid_to"' in upsert_form["$row"]
    assert (
        "- old note\\n\\n- `price` is in cents" in insert_form["$row"].replace("\\u0060", "`")
        or "- old note" in insert_form["$row"]
    )
    assert '"core": "# Schema' in insert_form["$row"]


async def test_walk_owned_first_annotation_without_overlay(write_settings: Settings) -> None:
    existing = {
        "id": "shop.orders@t0",
        "subject": "shop.orders",
        "type": "AsterixDB Dataset",
        "text": "# Schema\n",
        "valid_from": "t0",
    }
    cap = make_capturing_cc(write_settings, handler=_store_handler(existing))
    result = await run_memory_write(
        cap.client, write_settings, subject="shop.orders", text="fresh note"
    )
    assert result.structured["action"] == "annotated"
    insert_form = _form_of(cap.requests[-1])
    assert '"overlay": "fresh note\\n"' in insert_form["$row"]


async def test_duplicate_note_in_overlay_is_noop(write_settings: Settings) -> None:
    existing = {
        "subject": "shop.orders",
        "type": "AsterixDB Dataset",
        "text": "# Schema\n\nknown caveat\n",
        "core": "# Schema\n",
        "overlay": "known caveat\n",
    }
    cap = make_capturing_cc(write_settings, handler=_store_handler(existing))
    result = await run_memory_write(
        cap.client, write_settings, subject="shop.orders", text="known caveat"
    )
    assert result.structured["action"] == "unchanged"
    assert len(cap.requests) == 1  # only the lookup; nothing written


async def test_existing_note_superseded_on_new_text(write_settings: Settings) -> None:
    existing = {"subject": "team/glossary", "type": "Note", "text": "old", "id": "g@t0"}
    cap = make_capturing_cc(write_settings, handler=_store_handler(existing))
    result = await run_memory_write(cap.client, write_settings, subject="team/glossary", text="new")
    assert result.structured["action"] == "superseded"
    assert len(cap.requests) == 3


async def test_identical_note_text_is_noop(write_settings: Settings) -> None:
    existing = {"subject": "team/glossary", "type": "Note", "text": "same", "id": "g@t0"}
    cap = make_capturing_cc(write_settings, handler=_store_handler(existing))
    result = await run_memory_write(
        cap.client, write_settings, subject="team/glossary", text="same"
    )
    assert result.structured["action"] == "unchanged"
    assert len(cap.requests) == 1


def _annotated_existing(overlay: str) -> dict:
    return {
        "id": "shop.orders@t0",
        "subject": "shop.orders",
        "type": "AsterixDB Dataset",
        "core": "# Schema\n",
        "overlay": overlay,
        "text": "# Schema\n\n" + overlay,
        "valid_from": "t0",
    }


STALE_NOTE = "- `tags` is a comma-separated string, use split()"
CORRECTION = "- `tags` is an array of strings, unnest it directly"


async def test_replaces_retires_contradicted_overlay_line(write_settings: Settings) -> None:
    existing = _annotated_existing(f"{STALE_NOTE}\n\n- orders arrive out of order\n")
    cap = make_capturing_cc(write_settings, handler=_store_handler(existing))
    result = await run_memory_write(
        cap.client,
        write_settings,
        subject="shop.orders",
        text=CORRECTION,
        replaces="comma-separated string",
    )
    assert result.structured["action"] == "annotated"
    assert result.structured["retired"] == 1
    row = _form_of(cap.requests[-1])["$row"]
    assert "comma-separated" not in row
    assert "array of strings" in row
    assert "orders arrive out of order" in row


async def test_replaces_match_is_case_insensitive(write_settings: Settings) -> None:
    existing = _annotated_existing(f"{STALE_NOTE}\n")
    cap = make_capturing_cc(write_settings, handler=_store_handler(existing))
    result = await run_memory_write(
        cap.client,
        write_settings,
        subject="shop.orders",
        text=CORRECTION,
        replaces="COMMA-SEPARATED",
    )
    assert result.structured["retired"] == 1
    assert "comma-separated" not in _form_of(cap.requests[-1])["$row"]


async def test_replaces_without_match_still_annotates(write_settings: Settings) -> None:
    existing = _annotated_existing("- unrelated caveat\n")
    cap = make_capturing_cc(write_settings, handler=_store_handler(existing))
    result = await run_memory_write(
        cap.client,
        write_settings,
        subject="shop.orders",
        text=CORRECTION,
        replaces="no such line",
    )
    assert result.structured["action"] == "annotated"
    assert result.structured["retired"] == 0
    row = _form_of(cap.requests[-1])["$row"]
    assert "unrelated caveat" in row and "array of strings" in row


async def test_replaces_retires_stale_line_even_when_note_already_present(
    write_settings: Settings,
) -> None:
    # Without replaces this would be an "unchanged" no-op; the retirement is the change.
    existing = _annotated_existing(f"{STALE_NOTE}\n\n{CORRECTION}\n")
    cap = make_capturing_cc(write_settings, handler=_store_handler(existing))
    result = await run_memory_write(
        cap.client,
        write_settings,
        subject="shop.orders",
        text=CORRECTION,
        replaces="comma-separated string",
    )
    assert result.structured["action"] == "annotated"
    assert result.structured["retired"] == 1
    row = _form_of(cap.requests[-1])["$row"]
    assert "comma-separated" not in row
    assert row.count("array of strings") == 2  # once in overlay, once in merged text


async def test_replaces_on_standalone_note_is_ignored(write_settings: Settings) -> None:
    existing = {"subject": "team/glossary", "type": "Note", "text": "old", "id": "g@t0"}
    cap = make_capturing_cc(write_settings, handler=_store_handler(existing))
    result = await run_memory_write(
        cap.client, write_settings, subject="team/glossary", text="new", replaces="old"
    )
    assert result.structured["action"] == "superseded"
    assert result.structured["retired"] == 0


async def test_replaces_on_new_subject_retires_nothing(write_settings: Settings) -> None:
    cap = make_capturing_cc(write_settings, handler=_store_handler(None))
    result = await run_memory_write(
        cap.client, write_settings, subject="team/glossary", text="new", replaces="old"
    )
    assert result.structured["action"] == "created"
    assert result.structured["retired"] == 0


async def test_cc_failure_maps_to_tool_error(write_settings: Settings) -> None:
    cap = make_capturing_cc(
        write_settings,
        response_json={"status": "fatal", "errors": [{"code": 1, "msg": "boom"}]},
    )
    result = await run_memory_write(cap.client, write_settings, subject="dv.ds", text="note")
    assert result.structured["errorType"]


async def test_cc_client_write_guard_rejects_foreign_statements(
    write_settings: Settings, settings: Settings
) -> None:
    cap = make_capturing_cc(write_settings)
    with pytest.raises(GatewayError) as excinfo:
        await cap.client.execute_memory_write(
            "DELETE FROM Other.Data;", client_context_id="sess::t::1"
        )
    assert excinfo.value.error_type is ErrorType.FORBIDDEN
    assert cap.requests == []

    disabled = make_capturing_cc(settings)
    with pytest.raises(GatewayError) as excinfo:
        await disabled.client.execute_memory_write(
            "INSERT INTO AgentMemory.Memory ([$row]);", client_context_id="sess::t::1"
        )
    assert excinfo.value.error_type is ErrorType.FORBIDDEN
    assert disabled.requests == []
