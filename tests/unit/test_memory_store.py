"""Unit tests for the memory store accessor.

This module is the single place that knows where memory rows live and who may
read them. The last test here is the one that keeps it that way.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from asterixdb_mcp import memory_store
from asterixdb_mcp.memory_store import (
    GLOBAL_PRINCIPAL,
    MEMORY_DATASET,
    PRINCIPAL_FIELD,
    SELF_DATAVERSE,
    SESSION_EVENT_DATASET,
    scope_clause,
    tag,
)


def test_tag_stamps_the_owning_principal() -> None:
    tagged = tag({"id": "1"}, "tenant-a")

    assert tagged[PRINCIPAL_FIELD] == "tenant-a"


def test_tag_does_not_mutate_the_row_it_is_given() -> None:
    row = {"id": "1"}

    tag(row, "tenant-a")

    assert PRINCIPAL_FIELD not in row


def test_tag_overwrites_a_caller_supplied_principal() -> None:
    # The row body reaches here from tool arguments on some paths. A caller must
    # not be able to write a row that claims to belong to another tenant.
    tagged = tag({"id": "1", PRINCIPAL_FIELD: "tenant-b"}, "tenant-a")

    assert tagged[PRINCIPAL_FIELD] == "tenant-a"


def test_scope_clause_admits_the_owning_tenant_and_the_global_tier() -> None:
    clause = scope_clause("m")

    assert "m.principal = $principal" in clause
    assert f'm.principal = "{GLOBAL_PRINCIPAL}"' in clause


def test_scope_clause_is_parenthesised_so_it_can_be_ANDed_safely() -> None:
    # Without the parens, "A AND B OR C" would bind as "(A AND B) OR C" and the
    # global tier would satisfy the whole predicate on its own.
    clause = scope_clause("m")

    assert clause.startswith("(")
    assert clause.endswith(")")


def test_scope_clause_uses_a_bound_parameter_not_interpolation() -> None:
    # principal derives from a JWT claim; interpolating it into SQL++ would make
    # the token an injection vector.
    assert "$principal" in scope_clause("m")


def test_scope_clause_honours_the_alias_it_is_given() -> None:
    assert "e.principal" in scope_clause("e")


def test_global_principal_is_not_a_legal_client_id() -> None:
    # A tenant that could authenticate as GLOBAL_PRINCIPAL would read every
    # tenant's rows, so it must not be a value an OAuth client_id can take.
    assert GLOBAL_PRINCIPAL == "*"


def _memory_statements() -> list[tuple[str, str, str]]:
    """(module, attribute, statement) for every memory statement the code holds."""
    package = Path(memory_store.__file__).parent
    root = memory_store.__name__.rsplit(".", 1)[0]
    found: list[tuple[str, str, str]] = []
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).with_suffix("")
        name = f"{root}." + ".".join(relative.parts).removesuffix(".__init__")
        module = importlib.import_module(name)
        for attribute, value in vars(module).items():
            if isinstance(value, str) and SELF_DATAVERSE + "." in value:
                found.append((name, attribute, value))
    return found


def test_every_stored_memory_query_is_scoped_to_one_tenant() -> None:
    # Option A puts nothing between one tenant's rows and another's but the
    # predicate on the read. A query that names a memory dataset and forgets it
    # returns every tenant's rows.
    unscoped = [
        f"{module}.{attribute}"
        for module, attribute, statement in _memory_statements()
        if statement.lstrip().upper().startswith("SELECT")
        and "$principal" not in statement
        # The ownership backfill is the one read that must see unowned rows.
        # Restricting it to them is what makes it safe: a row nobody owns
        # cannot be another tenant's.
        and f"{PRINCIPAL_FIELD} IS UNKNOWN" not in statement
    ]

    assert unscoped == [], f"these memory queries read across every tenant: {unscoped}"


def test_the_statement_scan_actually_finds_the_queries_it_checks() -> None:
    # The test above passes vacuously if the scan finds nothing, so pin it to a
    # query that must always be there.
    assert any(attribute == "CURRENT_ROWS_QUERY" for _, attribute, _ in _memory_statements()), (
        "the memory-statement scan found no queries; the check above proves nothing"
    )


def test_only_the_store_module_names_the_memory_datasets() -> None:
    # Option A isolation is only as good as its chokepoint: if another module
    # writes its own query against these datasets, it will not carry the
    # principal predicate and will read across tenants.
    src = Path(memory_store.__file__).parent
    offenders: list[str] = []

    for path in sorted(src.rglob("*.py")):
        if path.name in {"memory_store.py", "config.py", "cc_client.py"}:
            continue
        body = path.read_text(encoding="utf-8")
        for line in body.splitlines():
            code = line.split("#", 1)[0]
            # Prose referring to the store is fine; the repo spells those as
            # ``AgentMemory.Memory`` in docstrings. Only real code is a leak.
            if "``" in code:
                continue
            if MEMORY_DATASET in code or SESSION_EVENT_DATASET in code:
                offenders.append(f"{path.relative_to(src)}: {line.strip()}")

    assert offenders == [], (
        "these modules name a memory dataset directly instead of going through "
        f"memory_store, so their queries carry no principal predicate: {offenders}"
    )
