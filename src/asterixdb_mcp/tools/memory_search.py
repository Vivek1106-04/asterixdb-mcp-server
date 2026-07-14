"""memory_search: structural + lexical retrieval over the OKF memory store.

The agentic-memory store (``Dashboard.Memory``) holds Open Knowledge Format
concept documents — schema knowledge, statistics, observed query patterns —
materialized from the engine's ``okf_catalog()`` walk and distilled facts. This
tool is the read side of that store, blending the three OKF retrieval paths in
priority order:

1. **Subject key** — exact ``subject`` match (the concept's stable identity,
   e.g. ``SalesDV.orders``): the precise path.
2. **Full text** — ``ftcontains`` over the concept body for lexical recall.
3. **Link graph** — up to ``link_depth`` hops across ``links`` from every hit,
   pulling in related concepts (a dataset's datatype, its indexes, and at
   depth 2 their neighbours in turn) for connected, multi-hop context.

Only *current* facts are returned (bi-temporal rows whose ``valid_to`` is
unset); superseded history stays out of context.

Defense-in-Depth:
- Layer 1: the schema documents the blend order and that ``subject`` must be an
  exact concept identity while ``query`` is free text.
- Layer 2: empty/over-long queries are rejected pre-flight with self-correcting
  messages; ``subject`` and dataverse scoping are validated against a strict
  identifier charset so no free text is ever interpolated into SQL++; full-text
  terms are reduced to plain alphanumeric tokens before use.
"""

from __future__ import annotations

import re
from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError
from . import ToolResult

DEFAULT_LIMIT = 8
MAX_LIMIT = 50
MAX_QUERY_LEN = 500
MAX_LINK_DEPTH = 2
# fetch window for the full-text pass before client-side ranking
FT_FETCH_WINDOW = 100

MEMORY_DATASET = "Dashboard.Memory"

# Concept identities are engine-emitted (dv.ds, dv.ds/index/x, dv/type/T ...):
# letters, digits and a small punctuation set. Anything else is rejected so no
# free text ever reaches a SQL++ literal.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.@/-]+$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

_CURRENT = "m.valid_to IS UNKNOWN"

_SUBJECT_QUERY = (
    f'SELECT VALUE m FROM {MEMORY_DATASET} m WHERE m.subject = "__SUBJECT__" AND {_CURRENT};'
)
_FULLTEXT_QUERY = (
    f"SELECT VALUE m FROM {MEMORY_DATASET} m "
    f'WHERE ftcontains(m.`text`, [__TOKENS__], {{"mode": "any"}}) AND {_CURRENT} '
    f"LIMIT {FT_FETCH_WINDOW};"
)
_LINKS_QUERY = (
    f"SELECT VALUE m FROM {MEMORY_DATASET} m WHERE m.subject IN [__SUBJECTS__] AND {_CURRENT};"
)

_DOC_FIELDS = ("subject", "type", "title", "description", "text", "links", "tags", "timestamp")


async def run_memory_search(
    client: CCClient,
    settings: Settings,
    *,
    query: str,
    subject: str | None = None,
    dataverse: str | None = None,
    limit: int = DEFAULT_LIMIT,
    follow_links: bool = True,
    link_depth: int = 1,
) -> ToolResult:
    """Retrieve current OKF concept docs by subject key, full text, and link hops."""
    needle = query.strip()
    if not needle and not subject:
        return ToolResult.error(
            GatewayError(
                ErrorType.INVALID_PARAMETER,
                "Provide a non-empty query (free text) and/or an exact subject.",
            )
        )
    if len(needle) > MAX_QUERY_LEN:
        return ToolResult.error(
            GatewayError(
                ErrorType.INVALID_PARAMETER,
                f"Query is too long (max {MAX_QUERY_LEN} characters).",
            )
        )
    for label, value in (("subject", subject), ("dataverse", dataverse)):
        if value is not None and not _IDENTIFIER_RE.match(value):
            return ToolResult.error(
                GatewayError(
                    ErrorType.INVALID_PARAMETER,
                    f"Invalid {label} {value!r}: expected a concept identifier "
                    "(letters, digits, '_', '.', '/', '@', '-').",
                )
            )
    limit = min(max(limit, 1), MAX_LIMIT)
    ccid = make_client_context_id(settings.agent_session_id, "memory_search")

    matches: list[dict[str, Any]] = []
    seen: set[str] = set()

    if subject:
        for doc in await _fetch(client, ccid, _SUBJECT_QUERY.replace("__SUBJECT__", subject)):
            _add(matches, seen, doc, via="subject")

    tokens = [t.lower() for t in _TOKEN_RE.findall(needle)]
    if tokens:
        token_list = ", ".join(f'"{t}"' for t in tokens)
        docs = await _fetch(client, ccid, _FULLTEXT_QUERY.replace("__TOKENS__", token_list))
        docs.sort(key=lambda d: -_score(d, tokens))
        for doc in docs:
            _add(matches, seen, doc, via="fulltext")

    if dataverse is not None:
        matches = [m for m in matches if str(m.get("subject", "")).startswith(dataverse)]
        seen = {str(m["subject"]) for m in matches}
    matches = matches[:limit]

    if follow_links and matches:
        link_depth = min(max(link_depth, 1), MAX_LINK_DEPTH)
        frontier = matches
        for hop in range(1, link_depth + 1):
            linked = _link_targets(frontier, seen)
            if not linked:
                break
            subject_list = ", ".join(f'"{s}"' for s in linked)
            docs = await _fetch(client, ccid, _LINKS_QUERY.replace("__SUBJECTS__", subject_list))
            frontier = []
            for doc in docs:
                if _add(matches, seen, doc, via="link" if hop == 1 else f"link-{hop}"):
                    frontier.append(matches[-1])

    structured = {
        "status": "success",
        "query": needle,
        "subject": subject,
        "limit": limit,
        "matches": matches,
    }
    if not matches:
        return ToolResult(
            text=(
                "No memory concepts matched. The store may not be materialized yet "
                "(run the OKF refresh), or try broader terms."
            ),
            structured=structured,
        )
    return ToolResult(
        text=f"{len(matches)} memory concept(s): "
        + ", ".join(str(m.get("subject")) for m in matches),
        structured=structured,
    )


async def _fetch(client: CCClient, ccid: str, statement: str) -> list[dict[str, Any]]:
    """Run one read; a failed pass degrades to no results instead of failing the blend."""
    try:
        envelope = await client.execute(statement, client_context_id=ccid)
    except GatewayError:
        return []
    return [row for row in envelope.get("results", []) if isinstance(row, dict)]


def _add(matches: list[dict[str, Any]], seen: set[str], doc: dict[str, Any], *, via: str) -> bool:
    subj = str(doc.get("subject", ""))
    if not subj or subj in seen:
        return False
    seen.add(subj)
    projected: dict[str, Any] = {k: doc[k] for k in _DOC_FIELDS if k in doc}
    projected["via"] = via
    matches.append(projected)
    return True


def _score(doc: dict[str, Any], tokens: list[str]) -> int:
    text = str(doc.get("text", "")).lower() + " " + str(doc.get("subject", "")).lower()
    return sum(text.count(token) for token in tokens)


def _link_targets(matches: list[dict[str, Any]], seen: set[str]) -> list[str]:
    """Collect valid, unseen link targets from the current match set."""
    out: list[str] = []
    for match in matches:
        for link in match.get("links", []) or []:
            target = str(link)
            if target not in seen and target not in out and _IDENTIFIER_RE.match(target):
                out.append(target)
    return out
