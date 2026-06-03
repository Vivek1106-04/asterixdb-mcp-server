"""list_functions and get_function: the SQL++ function catalog.

Gives an LLM a unified view over two function sources so it stops guessing names:
- built-ins (curated INTERNAL catalog, see builtins_catalog),
- user-defined functions read live from ``Metadata.Function`` (SQL++, Java, Python).

Defense-in-Depth:
- Layer 1: the language filter is a strict enum (INTERNAL / SQL++ / JAVA / PYTHON);
  the schema tells the model to use list_functions before referencing an
  unfamiliar function.
- Layer 2: get_function returns a self-correcting NOT_FOUND with near-name hints
  rather than an empty body, and flags external (Java/Python) UDFs as code that
  runs on the cluster.
"""

from __future__ import annotations

from typing import Any

from ..builtins_catalog import BUILTINS_BY_NAME, all_builtins
from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import ErrorType, GatewayError
from . import ToolResult

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

LANGUAGE_INTERNAL = "INTERNAL"
LANGUAGE_SQLPP = "SQL++"
LANGUAGE_JAVA = "JAVA"
LANGUAGE_PYTHON = "PYTHON"
LANGUAGES = (LANGUAGE_INTERNAL, LANGUAGE_SQLPP, LANGUAGE_JAVA, LANGUAGE_PYTHON)

_EXTERNAL_LANGUAGES = frozenset({LANGUAGE_JAVA, LANGUAGE_PYTHON})

_UDF_LIST_QUERY = "SELECT VALUE f FROM Metadata.`Function` f ORDER BY f.DataverseName, f.Name;"
_UDF_GET_QUERY = "SELECT VALUE f FROM Metadata.`Function` f WHERE f.Name = $name"


async def run_list_functions(
    client: CCClient,
    settings: Settings,
    *,
    language: str | None = None,
    name_contains: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> ToolResult:
    """List built-in and user-defined functions, filtered by language and name."""
    if language is not None and language not in LANGUAGES:
        return ToolResult.error(
            GatewayError(
                ErrorType.INVALID_PARAMETER,
                f"Unknown language {language!r}. Use one of: {', '.join(LANGUAGES)}.",
            )
        )
    offset = max(offset, 0)
    limit = min(max(limit, 1), MAX_LIMIT)
    needle = (name_contains or "").strip().lower()

    records = list(_builtin_records())
    records.extend(await _udf_records(client, settings))

    filtered = [
        r
        for r in records
        if (language is None or r["language"] == language)
        and (not needle or needle in r["name"].lower())
    ]
    window = filtered[offset : offset + limit]
    structured = {
        "status": "success",
        "total": len(filtered),
        "offset": offset,
        "limit": limit,
        "moreAvailable": offset + limit < len(filtered),
        "functions": window,
    }
    return ToolResult(
        text=f"{len(window)} of {len(filtered)} function(s).", structured=structured
    )


async def run_get_function(
    client: CCClient,
    settings: Settings,
    *,
    name: str,
    dataverse: str | None = None,
    user_tag: str | None = None,
) -> ToolResult:
    """Return one function's signature (and body, for a UDF) by name."""
    clean = name.strip()
    if not clean:
        return ToolResult.error(
            GatewayError(ErrorType.INVALID_PARAMETER, "Provide a function name.")
        )

    # A built-in only resolves when no dataverse is given (UDFs are dataverse-scoped).
    if dataverse is None:
        builtin = BUILTINS_BY_NAME.get(clean.lower())
        if builtin is not None:
            structured = {
                "status": "success",
                "scope": "builtin",
                "name": builtin.name,
                "language": LANGUAGE_INTERNAL,
                "category": builtin.category,
                "summary": builtin.summary,
            }
            return ToolResult(text=f"{builtin.name} — {builtin.summary}", structured=structured)

    return await _get_udf(client, settings, clean, dataverse, user_tag)


async def _get_udf(
    client: CCClient,
    settings: Settings,
    name: str,
    dataverse: str | None,
    user_tag: str | None,
) -> ToolResult:
    ccid = make_client_context_id(settings.agent_session_id, user_tag)
    query = _UDF_GET_QUERY
    params: dict[str, Any] = {"name": name}
    if dataverse is not None:
        query += " AND f.DataverseName = $dv"
        params["dv"] = dataverse
    try:
        envelope = await client.execute(
            query + ";", client_context_id=ccid, statement_parameters=params
        )
    except GatewayError as err:
        return ToolResult.error(err)

    rows = [r for r in (envelope.get("results") or []) if isinstance(r, dict)]
    if not rows:
        return ToolResult.error(
            GatewayError(
                ErrorType.NOT_FOUND,
                f"No function named {name!r} found. Call list_functions to discover exact "
                "names, or check the dataverse for a user-defined function.",
            )
        )

    record = _udf_detail(rows[0])
    text = f"{record['name']} ({record['language']} UDF), arity {record['arity']}."
    if record.get("safetyWarning"):
        text += " " + record["safetyWarning"]
    return ToolResult(text=text, structured={"status": "success", "scope": "udf", **record})


def _builtin_records() -> list[dict[str, Any]]:
    return [
        {
            "name": fn.name,
            "language": LANGUAGE_INTERNAL,
            "dataverse": None,
            "category": fn.category,
        }
        for fn in all_builtins()
    ]


async def _udf_records(client: CCClient, settings: Settings) -> list[dict[str, Any]]:
    ccid = make_client_context_id(settings.agent_session_id, "list_functions")
    try:
        envelope = await client.execute(_UDF_LIST_QUERY, client_context_id=ccid)
    except GatewayError:
        return []
    records: list[dict[str, Any]] = []
    for row in envelope.get("results") or []:
        if not isinstance(row, dict) or not isinstance(row.get("Name"), str):
            continue
        records.append(
            {
                "name": row["Name"],
                "language": _normalize_language(row.get("Language")),
                "dataverse": row.get("DataverseName"),
                "arity": row.get("Arity"),
            }
        )
    return records


def _udf_detail(row: dict[str, Any]) -> dict[str, Any]:
    language = _normalize_language(row.get("Language"))
    detail: dict[str, Any] = {
        "name": row.get("Name"),
        "dataverse": row.get("DataverseName"),
        "arity": row.get("Arity"),
        "language": language,
        "params": row.get("Params"),
        "returnType": row.get("ReturnType"),
        "definition": row.get("Definition"),
    }
    if language in _EXTERNAL_LANGUAGES:
        detail["safetyWarning"] = (
            f"This is an external {language} UDF: its body runs arbitrary code on the cluster. "
            "Review it before trusting its output."
        )
    return detail


def _normalize_language(raw: Any) -> str:
    """Map a Metadata Language value to the public enum form."""
    if not isinstance(raw, str):
        return LANGUAGE_SQLPP
    upper = raw.strip().upper()
    if upper in ("SQLPP", "SQL++"):
        return LANGUAGE_SQLPP
    if upper in (LANGUAGE_JAVA, LANGUAGE_PYTHON):
        return upper
    return upper or LANGUAGE_SQLPP
