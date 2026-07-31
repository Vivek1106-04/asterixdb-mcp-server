"""Unit tests for binding a CC client to one tenant.

Every memory row has to name the tenant that owns it, and the place that
guarantees it is the write path itself: a client that was never bound to a
tenant cannot write memory at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType, GatewayError
from asterixdb_mcp.memory_store import BOOTSTRAP_STATEMENTS, PRINCIPAL_FIELD, scope_clause
from tests.conftest import CapturingCC, make_capturing_cc

pytestmark = pytest.mark.anyio

_INSERT = "INSERT INTO AgentMemory.Memory ([$row]);"
_SCOPED_SELECT = f"SELECT VALUE m FROM AgentMemory.Memory m WHERE {scope_clause('m')};"


@pytest.fixture
def write_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"memory_write_enabled": True})


def _written_row(request: httpx.Request) -> dict:
    return json.loads(parse_qs(request.content.decode())["$row"][0])


def test_a_client_starts_unbound(write_settings: Settings) -> None:
    cap = make_capturing_cc(write_settings, principal=None)

    assert cap.client.principal is None


def test_for_principal_returns_a_client_bound_to_that_tenant(write_settings: Settings) -> None:
    cap = make_capturing_cc(write_settings, principal=None)

    assert cap.client.for_principal("tenant-a").principal == "tenant-a"


def test_for_principal_leaves_the_client_it_came_from_unbound(write_settings: Settings) -> None:
    cap = make_capturing_cc(write_settings, principal=None)

    cap.client.for_principal("tenant-a")

    assert cap.client.principal is None


def test_rebinding_a_bound_client_does_not_affect_the_first_binding(
    write_settings: Settings,
) -> None:
    cap = make_capturing_cc(write_settings, principal=None)
    first = cap.client.for_principal("tenant-a")

    second = first.for_principal("tenant-b")

    assert (first.principal, second.principal) == ("tenant-a", "tenant-b")


async def test_an_unbound_client_refuses_to_write_memory(write_settings: Settings) -> None:
    # An untagged row is readable by nobody once reads are filtered, so a
    # forgotten binding must fail loudly here rather than write into the void.
    cap = make_capturing_cc(write_settings, principal=None)

    with pytest.raises(GatewayError) as excinfo:
        await cap.client.execute_memory_write(
            _INSERT, client_context_id="sess::t::1", statement_parameters={"row": {"id": "1"}}
        )

    assert excinfo.value.error_type is ErrorType.INTERNAL
    assert cap.requests == []


async def test_a_written_row_carries_the_bound_principal(write_settings: Settings) -> None:
    cap = make_capturing_cc(write_settings, principal="tenant-a")

    await cap.client.execute_memory_write(
        _INSERT, client_context_id="sess::t::1", statement_parameters={"row": {"id": "1"}}
    )

    assert _written_row(cap.requests[-1])[PRINCIPAL_FIELD] == "tenant-a"


async def test_a_row_claiming_another_tenant_is_overwritten(write_settings: Settings) -> None:
    # Row bodies are built from tool arguments on some paths; a caller must not
    # be able to plant a row in another tenant's namespace.
    cap = make_capturing_cc(write_settings, principal="tenant-a")

    await cap.client.execute_memory_write(
        _INSERT,
        client_context_id="sess::t::1",
        statement_parameters={"row": {"id": "1", PRINCIPAL_FIELD: "tenant-b"}},
    )

    assert _written_row(cap.requests[-1])[PRINCIPAL_FIELD] == "tenant-a"


async def test_stamping_does_not_mutate_the_parameters_it_was_given(
    write_settings: Settings,
) -> None:
    cap = make_capturing_cc(write_settings, principal="tenant-a")
    row = {"id": "1"}
    parameters = {"row": row}

    await cap.client.execute_memory_write(
        _INSERT, client_context_id="sess::t::1", statement_parameters=parameters
    )

    assert row == {"id": "1"}
    assert parameters == {"row": {"id": "1"}}


async def test_bootstrap_ddl_still_runs_with_no_row_to_stamp(write_settings: Settings) -> None:
    cap = make_capturing_cc(write_settings, principal="tenant-a")

    await cap.client.execute_memory_write(BOOTSTRAP_STATEMENTS[0], client_context_id="sess::t::1")

    assert len(cap.requests) == 1


async def test_a_non_dict_row_parameter_is_left_alone(write_settings: Settings) -> None:
    # Nothing builds one today, but stamping must not crash on a shape it does
    # not understand — the statement guard above is what keeps writes narrow.
    cap = make_capturing_cc(write_settings, principal="tenant-a")

    await cap.client.execute_memory_write(
        _INSERT, client_context_id="sess::t::1", statement_parameters={"row": "not-an-object"}
    )

    assert json.loads(parse_qs(cap.requests[-1].content.decode())["$row"][0]) == "not-an-object"


# reads


async def test_a_memory_read_binds_the_tenant_from_the_client(write_settings: Settings) -> None:
    cap = make_capturing_cc(write_settings, principal="tenant-a")

    await cap.client.execute_memory_read(_SCOPED_SELECT, client_context_id="sess::t::1")

    form = parse_qs(cap.requests[-1].content.decode())
    assert json.loads(form["$principal"][0]) == "tenant-a"


async def test_a_caller_cannot_read_as_another_tenant(write_settings: Settings) -> None:
    # The tenant comes from the client's identity, never from the parameters a
    # caller passes, so a supplied principal is overwritten rather than honoured.
    cap = make_capturing_cc(write_settings, principal="tenant-a")

    await cap.client.execute_memory_read(
        _SCOPED_SELECT,
        client_context_id="sess::t::1",
        statement_parameters={PRINCIPAL_FIELD: "tenant-b"},
    )

    form = parse_qs(cap.requests[-1].content.decode())
    assert json.loads(form["$principal"][0]) == "tenant-a"


async def test_an_unbound_client_refuses_to_read_memory(write_settings: Settings) -> None:
    cap = make_capturing_cc(write_settings, principal=None)

    with pytest.raises(GatewayError) as excinfo:
        await cap.client.execute_memory_read(_SCOPED_SELECT, client_context_id="sess::t::1")

    assert excinfo.value.error_type is ErrorType.INTERNAL
    assert cap.requests == []


async def test_a_query_that_forgets_the_tenant_predicate_is_refused(
    write_settings: Settings,
) -> None:
    # Isolation is logical, so an unfiltered query reads every tenant's rows.
    cap = make_capturing_cc(write_settings, principal="tenant-a")

    with pytest.raises(GatewayError) as excinfo:
        await cap.client.execute_memory_read(
            "SELECT VALUE m FROM AgentMemory.Memory m;", client_context_id="sess::t::1"
        )

    assert excinfo.value.error_type is ErrorType.INTERNAL
    assert cap.requests == []


# every path that writes memory, checked end to end


def _rows_written(cap: CapturingCC) -> list[dict]:
    """Every row body the captured requests carried."""
    return [
        json.loads(form["$row"][0])
        for form in (parse_qs(request.content.decode()) for request in cap.requests)
        if "$row" in form
    ]


def _owners(cap: CapturingCC) -> set:
    return {row.get(PRINCIPAL_FIELD) for row in _rows_written(cap)}


def _store_handler(rows: list[dict] | None = None):
    """Answer catalog and store reads with ``rows``; acknowledge writes."""

    def handler(request: httpx.Request) -> httpx.Response:
        statement = parse_qs(request.content.decode())["statement"][0]
        body = rows if rows is not None and "SELECT" in statement else []
        return httpx.Response(200, json={"status": "success", "results": body})

    return handler


async def test_an_agent_note_is_owned_by_the_tenant_that_wrote_it(
    write_settings: Settings,
) -> None:
    from asterixdb_mcp.tools.memory_write import run_memory_write

    cap = make_capturing_cc(write_settings, handler=_store_handler(), principal="tenant-a")

    await run_memory_write(cap.client, write_settings, subject="dv.ds", text="ship_date is text")

    assert _owners(cap) == {"tenant-a"}


async def test_a_preference_is_owned_by_the_tenant_that_set_it(write_settings: Settings) -> None:
    from asterixdb_mcp.tools.preferences import run_remember_preference

    cap = make_capturing_cc(write_settings, handler=_store_handler(), principal="tenant-a")

    await run_remember_preference(cap.client, write_settings, text="project columns", scope="dv")

    assert _owners(cap) == {"tenant-a"}


async def test_walked_catalog_concepts_are_owned_by_the_walking_principal(
    write_settings: Settings,
) -> None:
    # Catalog facts name datasets, fields and indexes, so they are the tenant's
    # business data rather than shared reference material.
    from asterixdb_mcp.okf_walk import run_walk

    concept = {"subject": "dv.ds", "type": "AsterixDB Dataset", "text": "one dataset"}

    def handler(request: httpx.Request) -> httpx.Response:
        statement = parse_qs(request.content.decode())["statement"][0]
        hits = [concept] if "okf_catalog" in statement else []
        return httpx.Response(200, json={"status": "success", "results": hits})

    cap = make_capturing_cc(write_settings, handler=handler, principal="tenant-a")

    await run_walk(cap.client, write_settings)

    assert _owners(cap) == {"tenant-a"}


async def test_a_recorded_session_event_is_owned_by_the_tenant(write_settings: Settings) -> None:
    from asterixdb_mcp.tools.memory_capture import CaptureState, capture_query_outcome

    cap = make_capturing_cc(write_settings, handler=_store_handler(), principal="tenant-a")

    await capture_query_outcome(
        cap.client,
        write_settings,
        CaptureState(),
        statement="SELECT VALUE d FROM dv.ds d;",
        result_error=None,
    )

    assert _owners(cap) == {"tenant-a"}


async def test_an_archived_note_keeps_its_owner(write_settings: Settings) -> None:
    from asterixdb_mcp.decay import DECAY_AFTER_DAYS, run_decay

    stale = (datetime.now(timezone.utc) - timedelta(days=DECAY_AFTER_DAYS + 1)).isoformat()
    note = {"id": "n1", "subject": "dv.ds", "type": "Note", "valid_from": stale}
    cap = make_capturing_cc(write_settings, handler=_store_handler([note]), principal="tenant-a")

    await run_decay(cap.client, write_settings)

    assert _owners(cap) == {"tenant-a"}


async def test_a_reinforced_note_keeps_its_owner(write_settings: Settings) -> None:
    from asterixdb_mcp.tools.memory_notes import reinforce_notes

    cap = make_capturing_cc(write_settings, handler=_store_handler(), principal="tenant-a")

    await reinforce_notes(cap.client, "sess::t::1", [{"id": "n1", "subject": "dv.ds"}])

    assert _owners(cap) == {"tenant-a"}
