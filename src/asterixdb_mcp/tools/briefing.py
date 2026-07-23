"""Session-start briefing: prime the model before it writes its first query.

At the start of a session the model has named no dataset yet, so a full schema
dump would be mostly wasted context. Instead the first tool call of a session
carries one compact block: the cluster's dataverse/dataset inventory, the
active query-writing preferences, and a short set of how-to-query-here rules.
Per-dataset schema stays ambient — it attaches when a query or get_schema
actually touches that dataset.

The briefing is delivered at most once per session (``BriefingState``) and is
strictly best-effort: any failure to read the catalog or preferences degrades
to attaching nothing, and the flag is only spent when a block was actually
delivered, so a transient failure retries on the next call.
"""

from __future__ import annotations

from typing import Any

from ..cc_client import CCClient
from ..config import Settings
from ..context_id import make_client_context_id
from ..errors import GatewayError
from ..inventory import dataverse_names, fetch_dataset_rows
from . import ToolResult
from .get_schema import extract_dataset_format_info
from .preferences import GLOBAL_SCOPE, fetch_active_preferences

# Universal, cluster-agnostic SQL++ guidance. These hold regardless of dataset,
# so they ship as static rules rather than learned notes.
_STATIC_RULES = (
    "project the columns you need instead of SELECT * (essential on COLUMNAR datasets)",
    "quote reserved words and names with special characters in backticks",
    "add a WHERE filter so a secondary index can be used instead of a full scan",
)

_MAX_DATAVERSES_LISTED = 12


class BriefingState:
    """Session-scoped one-shot flag for the start-of-session briefing.

    ``pending()`` reports whether the briefing still needs delivering;
    ``mark()`` spends the flag. Only spent once a block is actually attached, so
    a failed first attempt does not suppress the briefing for the whole session.
    """

    def __init__(self) -> None:
        self._delivered = False

    def pending(self) -> bool:
        return not self._delivered

    def mark(self) -> None:
        self._delivered = True


def _columnar_count(rows: list[dict[str, Any]]) -> int:
    return sum(
        1 for row in rows if extract_dataset_format_info(row).get("format") == "COLUMNAR"
    )


def render_briefing(
    dataset_rows: list[dict[str, Any]], preferences: list[str]
) -> str:
    """Render the compact briefing block from inventory rows and preferences.

    Returns an empty string when there is nothing worth saying (no user
    datasets), so the caller attaches nothing rather than an empty header.
    """
    user_rows = [r for r in dataset_rows if r.get("DataverseName") != "Metadata"]
    if not user_rows:
        return ""
    dataverses = dataverse_names(user_rows)
    shown = dataverses[:_MAX_DATAVERSES_LISTED]
    more = len(dataverses) - len(shown)
    dv_label = ", ".join(shown) + (f", +{more} more" if more > 0 else "")
    columnar = _columnar_count(user_rows)

    lines = [
        "Session briefing (shown once):",
        f"- Dataverses ({len(dataverses)}): {dv_label}",
        f"- Datasets: {len(user_rows)}"
        + (f" ({columnar} COLUMNAR — project columns)" if columnar else ""),
        "- Query rules: " + "; ".join(_STATIC_RULES),
    ]
    if preferences:
        lines.append("- Preferences: " + "; ".join(preferences))
    lines.append(
        "Per-dataset schema and learned notes attach automatically when you query "
        "or inspect a dataset."
    )
    return "\n".join(lines)


async def build_briefing(client: CCClient, ccid: str) -> str:
    """Assemble the briefing text (best-effort; empty string on any failure)."""
    try:
        dataset_rows = await fetch_dataset_rows(client, ccid=ccid)
    except GatewayError:
        return ""
    preferences = await fetch_active_preferences(client, ccid, [GLOBAL_SCOPE])
    return render_briefing(dataset_rows, preferences)


async def maybe_attach_briefing(
    client: CCClient,
    settings: Settings,
    state: BriefingState,
    result: ToolResult,
) -> ToolResult:
    """Prepend the session briefing to ``result`` on the first call that can.

    No-op once the briefing has been delivered, when it renders empty, or when
    the memory surface is disabled. The briefing leads the text so the model
    reads the cluster orientation before the tool's own output.
    """
    if not settings.memory_enabled:
        return result
    if not state.pending():
        return result
    ccid = make_client_context_id(settings.agent_session_id, "briefing")
    briefing = await build_briefing(client, ccid)
    if not briefing:
        return result
    state.mark()
    return ToolResult(
        text=briefing + "\n\n" + result.text,
        structured=result.structured,
        is_error=result.is_error,
    )
