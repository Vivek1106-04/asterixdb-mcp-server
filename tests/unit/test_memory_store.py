"""Unit tests for the memory store accessor.

This module is the single place that knows where memory rows live and who may
read them. The last test here is the one that keeps it that way.
"""

from __future__ import annotations

from pathlib import Path

from asterixdb_mcp import memory_store
from asterixdb_mcp.memory_store import (
    GLOBAL_PRINCIPAL,
    MEMORY_DATASET,
    PRINCIPAL_FIELD,
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
