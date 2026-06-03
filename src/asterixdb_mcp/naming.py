"""Catalog name resolution.

Models get dataverse and dataset casing wrong or misremember names. This pure
helper resolves a supplied name against the real catalog (exact, then
case-insensitive, then fuzzy) so callers can correct the name or offer the
closest alternatives instead of returning a bare not-found.
"""

from __future__ import annotations

import difflib
import re

from .errors import ErrorType, GatewayError

_MAX_SUGGESTIONS = 5
_FUZZY_CUTOFF = 0.5
_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def quote_identifier(name: str) -> str:
    """Backtick-quote a catalog identifier after allowlist validation.

    Only ASCII letters, digits, and underscore are allowed. Anything else
    (dots, quotes, spaces, semicolons) is rejected, so a quoted identifier
    cannot break out of its context. Catalog-resolved names always pass.
    """
    if not _IDENT_RE.match(name):
        raise GatewayError(
            ErrorType.INVALID_PARAMETER,
            f"Identifier {name!r} contains characters outside [A-Za-z0-9_].",
        )
    return f"`{name}`"


def resolve(name: str, candidates: list[str]) -> tuple[str | None, list[str]]:
    """Resolve a possibly-misspelled name against known candidate names.

    Returns (canonical, suggestions). An exact or unique case-insensitive match
    returns the canonical name with no suggestions. Otherwise canonical is None
    and suggestions holds the closest names by edit distance.
    """
    if name in candidates:
        return name, []
    by_lower = {c.lower(): c for c in candidates}
    hit = by_lower.get(name.lower())
    if hit is not None:
        return hit, []
    suggestions = difflib.get_close_matches(
        name, candidates, n=_MAX_SUGGESTIONS, cutoff=_FUZZY_CUTOFF
    )
    return None, suggestions
