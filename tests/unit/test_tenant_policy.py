"""Unit tests for per-tenant dataverse scoping.

Authorization is decided on the *compiled plan*, not the statement text, because
the plan is post-resolution: a view, a synonym or an inlined UDF body appears as
the base datasets it actually reads. A regex over the statement sees none of that.
"""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType, GatewayError
from asterixdb_mcp.plan_parser import resolve_sources
from asterixdb_mcp.tenant_policy import (
    allowed_dataverses,
    authorize_statement,
    check_sources,
    is_enforced,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _settings(**allowlist: list[str]) -> Settings:
    return Settings(
        cc_base_url="http://test-cc:19002",
        agent_session_id="sess-test",
        dataverse_allowlist=allowlist or None,
    )


# H6: the resolver must not silently drop what it could not resolve


def test_a_qualified_source_resolves_to_its_dataverse() -> None:
    resolved = resolve_sources(("ShopDV.orders",), default_dataverse=None)

    assert resolved.datasets == (("ShopDV", "orders"),)
    assert resolved.unresolved == ()


def test_a_bare_source_takes_the_default_dataverse() -> None:
    resolved = resolve_sources(("orders",), default_dataverse="ShopDV")

    assert resolved.datasets == (("ShopDV", "orders"),)


def test_a_source_that_cannot_be_resolved_is_reported_not_dropped() -> None:
    # The whole of H6: a bare name with no default dataverse used to vanish from
    # the returned set, so a check over that set would pass without ever having
    # seen it.
    resolved = resolve_sources(("orders",), default_dataverse=None)

    assert resolved.datasets == ()
    assert resolved.unresolved == ("orders",)


def test_resolution_is_order_preserving_and_deduplicated() -> None:
    resolved = resolve_sources(("B.two", "A.one", "B.two"), default_dataverse=None)

    assert resolved.datasets == (("B", "two"), ("A", "one"))


def test_a_repeated_unresolvable_source_is_reported_once() -> None:
    resolved = resolve_sources(("orders", "orders"), default_dataverse=None)

    assert resolved.unresolved == ("orders",)


def test_an_index_suffix_does_not_change_the_dataset() -> None:
    resolved = resolve_sources(("ShopDV.orders.orderIdx",), default_dataverse=None)

    assert resolved.datasets == (("ShopDV", "orders"),)


# policy


def test_no_allowlist_means_no_enforcement() -> None:
    assert is_enforced(_settings()) is False


def test_configuring_any_tenant_turns_enforcement_on() -> None:
    assert is_enforced(_settings(**{"tenant-a": ["ShopDV"]})) is True


def test_an_unconfigured_gateway_authorizes_nothing_and_denies_nothing() -> None:
    # Single-tenant deployments must behave exactly as they did before, including
    # for a plan whose sources could not be resolved.
    check_sources(_settings(), None, resolve_sources(("orders", "BankDV.ledger"), None))


def test_a_tenant_may_read_its_own_dataverses() -> None:
    settings = _settings(**{"tenant-a": ["ShopDV"]})

    check_sources(settings, "tenant-a", resolve_sources(("ShopDV.orders",), None))


def test_a_tenant_may_not_read_another_tenants_dataverse() -> None:
    settings = _settings(**{"tenant-a": ["ShopDV"], "tenant-b": ["BankDV"]})

    with pytest.raises(GatewayError) as excinfo:
        check_sources(settings, "tenant-a", resolve_sources(("BankDV.ledger",), None))

    assert excinfo.value.error_type is ErrorType.FORBIDDEN
    assert "BankDV" in excinfo.value.message


def test_a_tenant_nobody_configured_is_denied_everything() -> None:
    # Fail closed. Once an allowlist exists, a principal missing from it is an
    # operator oversight, and guessing "allow" is the one guess that cannot be
    # walked back.
    settings = _settings(**{"tenant-a": ["ShopDV"]})

    with pytest.raises(GatewayError):
        check_sources(settings, "tenant-b", resolve_sources(("ShopDV.orders",), None))


def test_an_unresolvable_source_is_denied() -> None:
    # H6 as an authorization rule: a source we cannot name is a source we cannot
    # check, and an unchecked source must not execute.
    settings = _settings(**{"tenant-a": ["ShopDV"]})

    with pytest.raises(GatewayError) as excinfo:
        check_sources(settings, "tenant-a", resolve_sources(("orders",), None))

    assert "orders" in excinfo.value.message


def test_the_denial_names_every_dataverse_the_plan_touched() -> None:
    # An agent that is told only "denied" cannot tell a typo from a boundary.
    settings = _settings(**{"tenant-a": ["ShopDV"]})

    with pytest.raises(GatewayError) as excinfo:
        check_sources(
            settings, "tenant-a", resolve_sources(("ShopDV.orders", "BankDV.ledger"), None)
        )

    assert "BankDV" in excinfo.value.message
    assert "ShopDV" in excinfo.value.message


def test_a_plan_touching_nothing_is_allowed() -> None:
    # SELECT 1; reads no dataset. There is nothing to authorize.
    settings = _settings(**{"tenant-a": ["ShopDV"]})

    check_sources(settings, "tenant-a", resolve_sources((), None))


def test_an_unattributed_caller_is_denied_under_an_allowlist() -> None:
    settings = _settings(**{"tenant-a": ["ShopDV"]})

    with pytest.raises(GatewayError):
        check_sources(settings, None, resolve_sources(("ShopDV.orders",), None))


def test_allowed_dataverses_reports_the_configured_set() -> None:
    settings = _settings(**{"tenant-a": ["ShopDV", "RefDV"]})

    assert allowed_dataverses(settings, "tenant-a") == frozenset({"ShopDV", "RefDV"})


def test_allowed_dataverses_is_empty_for_an_unknown_tenant() -> None:
    settings = _settings(**{"tenant-a": ["ShopDV"]})

    assert allowed_dataverses(settings, "tenant-b") == frozenset()


# the compile-then-check path


def _plan_handler(sources: list[str] | None = None, errors: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if errors is not None:
            return httpx.Response(200, json={"status": "fatal", "errors": errors})
        if sources is None:
            return httpx.Response(200, json={"status": "success"})
        operator = {
            "operator": "data-scan",
            "operatorId": "1",
            "physical-operator": "DATASOURCE_SCAN",
            "data-source": sources[0],
        }
        plan = {"optimizedLogicalPlan": operator}
        return httpx.Response(200, json={"status": "success", "plans": plan})

    return handler


async def test_an_allowed_plan_passes_and_costs_one_compile() -> None:
    settings = _settings(**{"tenant-a": ["ShopDV"]})
    cap = make_capturing_cc(
        settings, handler=_plan_handler(["ShopDV.orders"]), principal="tenant-a"
    )

    await authorize_statement(
        cap.client, settings, statement="SELECT 1;", dataverse=None, ccid="sess::t::1"
    )

    assert len(cap.requests) == 1


async def test_a_denied_plan_never_executes() -> None:
    settings = _settings(**{"tenant-a": ["ShopDV"]})
    cap = make_capturing_cc(
        settings, handler=_plan_handler(["BankDV.ledger"]), principal="tenant-a"
    )

    with pytest.raises(GatewayError) as excinfo:
        await authorize_statement(
            cap.client, settings, statement="SELECT 1;", dataverse=None, ccid="sess::t::1"
        )

    assert excinfo.value.error_type is ErrorType.FORBIDDEN


async def test_nothing_is_compiled_when_no_allowlist_is_configured() -> None:
    # The extra round trip is only paid where somebody asked for the boundary.
    settings = _settings()
    cap = make_capturing_cc(settings, handler=_plan_handler(["BankDV.ledger"]))

    await authorize_statement(
        cap.client, settings, statement="SELECT 1;", dataverse=None, ccid="sess::t::1"
    )

    assert cap.requests == []


async def test_a_statement_that_will_not_compile_is_left_to_report_itself() -> None:
    # Denying here would dress a syntax error up as an authorization failure. A
    # statement that does not compile reads nothing, so letting execution surface
    # the real error is both safe and more useful.
    settings = _settings(**{"tenant-a": ["ShopDV"]})
    cap = make_capturing_cc(
        settings, handler=_plan_handler(errors=[{"msg": "Syntax error"}]), principal="tenant-a"
    )

    await authorize_statement(
        cap.client, settings, statement="SELEKT 1;", dataverse=None, ccid="sess::t::1"
    )


async def test_a_compile_that_yields_no_plan_is_denied() -> None:
    # Fail closed: no plan means nothing to authorize, and running it anyway would
    # make the boundary conditional on the engine's output shape.
    settings = _settings(**{"tenant-a": ["ShopDV"]})
    cap = make_capturing_cc(settings, handler=_plan_handler(), principal="tenant-a")

    with pytest.raises(GatewayError) as excinfo:
        await authorize_statement(
            cap.client, settings, statement="SELECT 1;", dataverse=None, ccid="sess::t::1"
        )

    assert excinfo.value.error_type is ErrorType.FORBIDDEN
