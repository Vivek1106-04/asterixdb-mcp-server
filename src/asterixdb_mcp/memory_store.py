"""Where memory rows live, and who is allowed to read them.

Memory is split in two tiers, along the line between facts about the *engine*
and facts about the *data*:

* **Global** — SQL++ syntax rules, builtin function documentation, error codes,
  index-type reference. Identical on every cluster, derived from no user's data,
  and carrying no PII by construction. Stored once under ``GLOBAL_PRINCIPAL`` and
  readable by every tenant.
* **Tenant** — catalog facts, agent-written notes, session events, distilled
  fixes, preferences. All of these describe a tenant's data, and catalog facts
  are no exception: dataset names, field names and index definitions are
  business-sensitive, so they are scoped like everything else rather than shared.

Isolation is logical rather than physical: one ``AgentMemory`` dataverse, with a
``principal`` field on every row and a predicate on every read. That choice is
only safe if the predicate is impossible to forget, so this module is the single
place that names the datasets, and ``test_memory_store`` fails the build if any
other module writes its own query against them. A leak becomes a red test rather
than something a reviewer has to catch.

Migrating to a dataverse per tenant later stays mechanical for the same reason:
nothing outside this module knows where the rows are.
"""

from __future__ import annotations

from typing import Any

SELF_DATAVERSE = "AgentMemory"
MEMORY_DATASET = "AgentMemory.Memory"
SESSION_EVENT_DATASET = "AgentMemory.SessionEvent"

# The only statement shapes the memory write path will pass through.
MEMORY_WRITE_PREFIXES = (
    "INSERT INTO AgentMemory.Memory",
    "UPSERT INTO AgentMemory.Memory",
    "INSERT INTO AgentMemory.SessionEvent",
)

# Idempotent store DDL. Kept as exact canonical strings: the CC client's memory
# write path allows precisely these statements (byte-for-byte) and nothing else
# beyond INSERT/UPSERT, so automatic bootstrap cannot become a DDL side door.
BOOTSTRAP_STATEMENTS = (
    "CREATE DATAVERSE AgentMemory IF NOT EXISTS;",
    "CREATE TYPE AgentMemory.MemoryType IF NOT EXISTS AS OPEN { id: string };",
    "CREATE DATASET AgentMemory.Memory(MemoryType) IF NOT EXISTS PRIMARY KEY id;",
    "CREATE INDEX memSubject IF NOT EXISTS ON AgentMemory.Memory(subject: string?) ENFORCED;",
    "CREATE INDEX memText IF NOT EXISTS ON AgentMemory.Memory(`text`: string?) "
    "TYPE FULLTEXT ENFORCED;",
    # Every read is filtered by tenant, so the principal predicate is on the hot
    # path of all of them and wants an index of its own.
    "CREATE INDEX memPrincipal IF NOT EXISTS ON AgentMemory.Memory(principal: string?) ENFORCED;",
    # episodic query-outcome events the gateway records for distillation
    "CREATE TYPE AgentMemory.SessionEventType IF NOT EXISTS AS OPEN { id: string };",
    "CREATE DATASET AgentMemory.SessionEvent(SessionEventType) IF NOT EXISTS PRIMARY KEY id;",
    "CREATE INDEX evtPrincipal IF NOT EXISTS ON AgentMemory.SessionEvent(principal: string?) "
    "ENFORCED;",
)

# The field every row carries, naming the tenant it belongs to.
PRINCIPAL_FIELD = "principal"

# The shared tier. Deliberately a character that cannot appear in an OAuth
# client_id, so no tenant can ever authenticate as the global reader.
GLOBAL_PRINCIPAL = "*"


def tag(row: dict[str, Any], principal: str) -> dict[str, Any]:
    """A copy of ``row`` stamped with its owning tenant.

    Overwrites any principal already present: on some paths the row body is
    built from tool arguments, and a caller must not be able to write a row
    that claims to belong to somebody else.
    """
    return {**row, PRINCIPAL_FIELD: principal}


def scope_clause(alias: str) -> str:
    """The predicate restricting a read to one tenant plus the global tier.

    Returned parenthesised so it survives being ``AND``-ed onto an existing
    ``WHERE``: unbracketed, ``A AND B OR C`` binds as ``(A AND B) OR C`` and a
    global row would satisfy the whole predicate on its own.

    The tenant is bound as ``$principal`` rather than interpolated — it comes
    from a JWT claim, and interpolating it would turn the token into an
    injection vector.
    """
    field = f"{alias}.{PRINCIPAL_FIELD}"
    return f'({field} = $principal OR {field} = "{GLOBAL_PRINCIPAL}")'
