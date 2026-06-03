"""Statement-level pre-flight guardrails (Defense-in-Depth, Layer 2).

Shallow, syntactic checks applied to a SQL++ statement BEFORE it is forwarded to
the AsterixDB Cluster Controller. These catch the cheap, high-frequency LLM
mistakes (a SET prefix the model was told not to send, a forgotten LIMIT, a
hallucinated aggregate name) so a malformed query is corrected — or rejected with
a self-correcting hint — gateway-side instead of erroring at the cluster.

This is deliberately NOT a SQL++ parser. It never rewrites the query body beyond
stripping a leading SET and appending a LIMIT. Semantic validation stays the CC's
job (and is exposed cheaply through validate_syntax / explain_query).
"""

from __future__ import annotations

import re

from .errors import ErrorType, GatewayError

_SET_PREFIX_RE = re.compile(r"(?i)^\s*set\s+[^;]+;\s*")
_LIMIT_RE = re.compile(r"(?i)\blimit\b")
_FROM_RE = re.compile(r"(?i)\bfrom\b")

# Hallucinated aggregate -> the real AsterixDB name(s). AsterixDB has no STDEV,
# bare STDDEV, or VARIANCE; it uses the SQL-standard sample/population variants.
# Matching the call form (NAME followed by "(") avoids flagging a field or alias
# that merely contains the text. Bare STDDEV( is flagged, but STDDEV_SAMP( is not
# (the "_" breaks the \s*\( match).
_UNSUPPORTED_FUNCTIONS: dict[str, str] = {
    "STDEV": "STDDEV_SAMP (sample) or STDDEV_POP (population)",
    "STDDEV": "STDDEV_SAMP (sample) or STDDEV_POP (population)",
    "VARIANCE": "VAR_SAMP (sample) or VAR_POP (population)",
}
_UNSUPPORTED_FUNCTION_RES: dict[str, re.Pattern[str]] = {
    name: re.compile(rf"(?i)\b{name}\s*\(") for name in _UNSUPPORTED_FUNCTIONS
}


def strip_set_prefix(statement: str) -> str:
    """Remove leading ``SET ...;`` clauses (the model is told to use compilerParameters)."""
    core = statement.strip()
    while True:
        match = _SET_PREFIX_RE.match(core)
        if match is None:
            return core
        core = core[match.end() :].lstrip()


def normalize_statement(statement: str, default_limit: int) -> str:
    """Drop leading SET clauses and ensure an unbounded SELECT...FROM has a LIMIT.

    Syntactic only; the query body is never rewritten. Returns the original
    statement unchanged if stripping leaves it empty.
    """
    core = strip_set_prefix(statement).rstrip()
    if core.endswith(";"):
        core = core[:-1].rstrip()
    if not core:
        return statement
    if core[:6].lower() == "select" and _FROM_RE.search(core) and _LIMIT_RE.search(core) is None:
        core = f"{core} LIMIT {default_limit}"
    return core + ";"


def check_unsupported_functions(statement: str) -> GatewayError | None:
    """Return a self-correcting error if the statement calls a known-bad aggregate.

    Layer 2 of Defense-in-Depth: caught here so the LLM gets an actionable hint
    without a wasted round-trip to the cluster. Returns None when nothing matches.
    """
    for name, pattern in _UNSUPPORTED_FUNCTION_RES.items():
        if pattern.search(statement):
            return GatewayError(
                ErrorType.INVALID_PARAMETER,
                f"{name}(...) is not an AsterixDB function. Use "
                f"{_UNSUPPORTED_FUNCTIONS[name]} instead, and do not nest an aggregate "
                "inside another aggregate in the same SELECT.",
            )
    return None
