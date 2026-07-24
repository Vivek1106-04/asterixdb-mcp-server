"""remember_preference: durable query-writing rules and stylistic preferences.

A preference is not a fact about the data — it is guidance on *how* to write
queries against this cluster: "always project columns on COLUMNAR datasets",
"quote reserved words in backticks", "prefer the CA dataverse". These outlive
any single dataset and steer the model toward correct, efficient SQL++ from the
first attempt.

Preferences live in the same ``AgentMemory.Memory`` store as OKF concepts but
carry ``type="Preference"`` / ``kind="preference"``, which sets them apart from
data-facts:

- they are never evidence-scored (a style rule has no proving query),
- the decay pass leaves them alone (a rule does not go stale from disuse),
- they attach to the session-start briefing rather than to a single dataset.

Scope is either ``global`` (every query) or a dataverse name (rules for that
dataverse only). Storage is bi-temporal for a uniform history, but there is no
overlay/core split: a preference is a flat, standalone line.

Defense-in-Depth:
- Layer 1: the schema says writes are gated by ``memory_write_enabled``.
- Layer 2: the text is length-capped, the scope is validated against the strict
  identifier charset, and the row is bound as a statement parameter — no client
  text is spliced into SQL++. The CC client re-checks the write gate and that
  the statement targets the AgentMemory store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError
from . import ToolResult
from .memory_search import _IDENTIFIER_RE, MEMORY_DATASET

PREFERENCE_TYPE = "Preference"
PREFERENCE_KIND = "preference"
GLOBAL_SCOPE = "global"
MAX_PREF_LEN = 500

# subject namespace keeps preferences out of the dataset/concept key space so a
# scope fetch is a cheap prefix-free equality on a reserved subject.
_SUBJECT_PREFIX = "_pref/"

_CURRENT_QUERY = (
    f"SELECT VALUE m FROM {MEMORY_DATASET} m WHERE m.subject = $subject AND m.valid_to IS UNKNOWN;"
)
_ACTIVE_QUERY = (
    f"SELECT VALUE m FROM {MEMORY_DATASET} m "
    "WHERE m.subject IN $subjects AND m.valid_to IS UNKNOWN;"
)
_INSERT = f"INSERT INTO {MEMORY_DATASET} ([$row]);"


def preference_subject(scope: str) -> str:
    """The reserved memory subject that holds a scope's preferences."""
    return f"{_SUBJECT_PREFIX}{scope}"


async def run_remember_preference(
    client: CCClient,
    settings: Settings,
    *,
    text: str,
    scope: str = GLOBAL_SCOPE,
    author: str | None = None,
) -> ToolResult:
    """Persist one query-writing preference for ``scope`` (deduplicated)."""
    if not settings.memory_write_enabled:
        return ToolResult.error(
            GatewayError(
                ErrorType.FORBIDDEN,
                "Memory writes are disabled on this gateway. Ask the operator to set "
                "ASTERIXDB_MCP_MEMORY_WRITE_ENABLED=true.",
            )
        )
    rule = text.strip()
    if not rule:
        return ToolResult.error(
            GatewayError(ErrorType.INVALID_PARAMETER, "Provide a non-empty preference text.")
        )
    if len(rule) > MAX_PREF_LEN:
        return ToolResult.error(
            GatewayError(
                ErrorType.INVALID_PARAMETER,
                f"Preference text is too long (max {MAX_PREF_LEN} characters).",
            )
        )
    scope = scope.strip() or GLOBAL_SCOPE
    if not _IDENTIFIER_RE.match(scope):
        return ToolResult.error(
            GatewayError(
                ErrorType.INVALID_PARAMETER,
                f"Invalid scope {scope!r}: use 'global' or a dataverse name "
                "(letters, digits, '_', '.', '/', '@', '-').",
            )
        )

    subject = preference_subject(scope)
    ccid = make_client_context_id(settings.agent_session_id, "remember_preference")
    try:
        current = await _fetch_current(client, ccid, subject)
        if any(str(row.get("text", "")).strip() == rule for row in current):
            return ToolResult(
                text=f"Preference for scope '{scope}' already recorded; nothing written.",
                structured={
                    "status": "success",
                    "scope": scope,
                    "action": "unchanged",
                    "id": None,
                },
            )
        now = datetime.now(timezone.utc).isoformat()
        row: dict[str, Any] = {
            "id": f"{subject}@{now}",
            "subject": subject,
            "type": PREFERENCE_TYPE,
            "kind": PREFERENCE_KIND,
            "scope": scope,
            "text": rule,
            "valid_from": now,
        }
        if author:
            row["author"] = author
        await client.execute_memory_write(
            _INSERT, client_context_id=ccid, statement_parameters={"row": row}
        )
    except GatewayError as err:
        return ToolResult.error(err)
    return ToolResult(
        text=f"Preference recorded for scope '{scope}' ({row['id']}).",
        structured={
            "status": "success",
            "scope": scope,
            "action": "created",
            "id": row["id"],
        },
    )


async def _fetch_current(client: CCClient, ccid: str, subject: str) -> list[dict[str, Any]]:
    envelope = await client.execute(
        _CURRENT_QUERY, client_context_id=ccid, statement_parameters={"subject": subject}
    )
    return [row for row in envelope.get("results", []) if isinstance(row, dict)]


async def fetch_active_preferences(client: CCClient, ccid: str, scopes: list[str]) -> list[str]:
    """Current preference texts for the given scopes (best-effort, deduplicated).

    Any store failure degrades to an empty list so a briefing that cannot read
    preferences still renders the rest of its content.
    """
    subjects = [preference_subject(s) for s in scopes if s and _IDENTIFIER_RE.match(s)]
    if not subjects:
        return []
    try:
        envelope = await client.execute(
            _ACTIVE_QUERY, client_context_id=ccid, statement_parameters={"subjects": subjects}
        )
    except GatewayError:
        return []
    seen: list[str] = []
    for row in envelope.get("results", []):
        if not isinstance(row, dict):
            continue
        rule = str(row.get("text", "")).strip()
        if rule and rule not in seen:
            seen.append(rule)
    return seen
