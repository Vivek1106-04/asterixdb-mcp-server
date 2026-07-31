"""Which dataverses a tenant's agent is allowed to read.

Isolation of the memory store keeps one tenant's *notes* away from another. This
keeps one tenant's *data* away from another, and it is the last plane the gateway
can close on its own: the engine has no authorization, so anything reaching the CC
is fully privileged, and the only place a boundary can exist is here.

Two decisions shape the module.

**Authorization is decided on the compiled plan, never on statement text.** The
optimized plan is post-resolution, so a view, a synonym or an inlined UDF body
appears as the base datasets it actually reads. A regex over the statement sees the
view's name and nothing behind it, which makes statement-text checks not merely
weaker but wrong in the one case that matters.

**Everything unclear is a denial.** A principal missing from a configured
allowlist, a caller with no principal at all, a plan source we could not resolve to
a dataverse — each is refused. The gateway cannot fall back on the engine to catch
what it lets through, so "allow" is the one guess that cannot be walked back.

Unset, the allowlist disables enforcement entirely. That keeps a single-tenant
deployment working exactly as before and, more usefully, means the extra compile
this costs is only paid where somebody asked for the boundary.
"""

from __future__ import annotations

from typing import Any

from .cc_client import CCClient
from .config import Settings
from .errors import ErrorType, GatewayError
from .plan_parser import ResolvedSources, parse_optimized_plan, resolve_sources


def is_enforced(settings: Settings) -> bool:
    """Whether any tenant scoping is configured at all."""
    return bool(settings.dataverse_allowlist)


def allowed_dataverses(settings: Settings, principal: str | None) -> frozenset[str]:
    """The dataverses this principal may read. Empty means none."""
    allowlist = settings.dataverse_allowlist or {}
    if principal is None:
        return frozenset()
    return frozenset(allowlist.get(principal, ()))


def check_sources(settings: Settings, principal: str | None, resolved: ResolvedSources) -> None:
    """Authorize a compiled plan's data sources for this tenant.

    Raises:
        GatewayError: FORBIDDEN when the plan reads anything outside the tenant's
            allowlist, or anything we could not attribute to a dataverse.
    """
    if not is_enforced(settings):
        return

    if resolved.unresolved:
        # A source we cannot name is a source we cannot check, and an unchecked
        # source must not execute. This is the fail-closed half of H6.
        names = ", ".join(sorted(resolved.unresolved))
        raise GatewayError(
            ErrorType.FORBIDDEN,
            f"The compiled plan reads sources this gateway could not attribute to a "
            f"dataverse ({names}), so they cannot be authorized. Qualify every name as "
            "Dataverse.Dataset, or pass a default dataverse.",
        )

    allowed = allowed_dataverses(settings, principal)
    touched = {dataverse for dataverse, _ in resolved.datasets}
    denied = sorted(touched - allowed)
    if not denied:
        return

    # Name what was read and what was permitted: an agent told only "denied"
    # cannot tell a typo from a boundary, and will usually retry the same query.
    permitted = ", ".join(sorted(allowed)) or "none"
    raise GatewayError(
        ErrorType.FORBIDDEN,
        f"This client is not permitted to read {', '.join(denied)}. The compiled plan "
        f"reads {', '.join(sorted(touched))}; permitted dataverses are {permitted}. "
        "Note that views, synonyms and UDFs are resolved to the datasets they read, "
        "so a permitted name can still reach an unpermitted one.",
    )


def check_compiled_plan(
    settings: Settings,
    principal: str | None,
    envelope: dict[str, Any],
    dataverse: str | None,
) -> None:
    """Authorize a compile-only envelope the caller already has.

    Two query paths compile before executing anyway — for the columnar-scan
    advisory and for async submission — so authorizing from their envelope keeps
    tenant scoping free of an extra round trip on those paths.

    Raises:
        GatewayError: FORBIDDEN when the plan is not authorized for this tenant.
    """
    if not is_enforced(settings):
        return
    if envelope.get("errors"):
        # A statement that will not compile reads nothing, so there is nothing to
        # authorize. Returning lets execution surface the real syntax or semantic
        # error instead of dressing it up as an authorization failure.
        return

    parsed = parse_optimized_plan(envelope.get("plans"))
    if parsed is None:
        raise GatewayError(
            ErrorType.FORBIDDEN,
            "This gateway could not obtain a compiled plan for the statement, so it "
            "cannot authorize what the statement would read. Nothing was executed.",
        )

    check_sources(settings, principal, resolve_sources(parsed.data_sources, dataverse))


async def authorize_statement(
    client: CCClient,
    settings: Settings,
    *,
    statement: str,
    dataverse: str | None,
    ccid: str,
) -> None:
    """Compile without executing, then authorize what the plan reads.

    For callers with no compiled plan in hand. Costs one extra round trip, and
    only where an allowlist exists — with none configured this returns before
    touching the network.

    Raises:
        GatewayError: FORBIDDEN when the plan is not authorized for this tenant.
    """
    if not is_enforced(settings):
        return
    envelope = await client.compile_query(
        statement, client_context_id=ccid, dataverse=dataverse, emit_plan=True
    )
    check_compiled_plan(settings, client.principal, envelope, dataverse)
