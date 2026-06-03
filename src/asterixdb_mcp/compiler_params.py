"""compilerParameters allowlist and validation.

The execute and async query tools accept a ``compilerParameters`` object so an
LLM can tune a single query (memory budgets, parallelism, optimizer toggles).
These map to AsterixDB CC compiler options forwarded as ``/query/service`` form
fields, NOT inline ``SET`` clauses.

Only an explicit allowlist is forwarded. An unknown key, a wrong type, or an
out-of-range value is rejected gateway-side with ``INVALID_PARAMETER`` so a
malformed knob never reaches the cluster. The same allowlist backs the
``asterixdb://config-parameters`` resource, so what the LLM is told it may set
and what the gateway actually accepts can never drift.

Keys and their CC option names are verified against AsterixDB
``CompilerProperties.java`` (``ini()`` form: lowercased option name with ``_``
replaced by ``.``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import ErrorType, GatewayError

# Memory-budget bounds (bytes). The CC enforces a 512 KiB floor on the sort,
# join, group, and window operator budgets; we mirror that floor and cap the
# ceiling at 2 GiB so a single query can't be told to reserve an absurd budget.
_MIN_OPERATOR_MEMORY_BYTES = 512 * 1024
_MAX_OPERATOR_MEMORY_BYTES = 2 * 1024 * 1024 * 1024

# Parallelism: 0 means "use storage parallelism"; positive values pin the
# partition count. The CC itself clamps unreasonable values, but we bound the
# accepted range so the knob stays sane from the LLM's side.
_MIN_PARALLELISM = 0
_MAX_PARALLELISM = 4096


class ParamKind(str, Enum):
    """The validation shape of an allowlisted compiler parameter."""

    BOOLEAN = "boolean"
    INTEGER = "integer"
    BYTES = "bytes"


@dataclass(frozen=True)
class ParamSpec:
    """An allowlisted compiler parameter: its CC key, kind, range, and purpose."""

    kind: ParamKind
    description: str
    minimum: int | None = None
    maximum: int | None = None


# The allowlist. Keys are the exact CC form-field names. Anything not here is
# rejected. This is intentionally a safe, useful subset of CompilerProperties,
# not the full surface.
ALLOWLIST: dict[str, ParamSpec] = {
    "compiler.parallelism": ParamSpec(
        ParamKind.INTEGER,
        "Query execution parallelism. 0 uses the storage parallelism; a positive "
        "value pins the number of parallel partitions.",
        minimum=_MIN_PARALLELISM,
        maximum=_MAX_PARALLELISM,
    ),
    "compiler.sortmemory": ParamSpec(
        ParamKind.BYTES,
        "Memory budget in bytes for a sort operator instance in a partition.",
        minimum=_MIN_OPERATOR_MEMORY_BYTES,
        maximum=_MAX_OPERATOR_MEMORY_BYTES,
    ),
    "compiler.joinmemory": ParamSpec(
        ParamKind.BYTES,
        "Memory budget in bytes for a join operator instance in a partition.",
        minimum=_MIN_OPERATOR_MEMORY_BYTES,
        maximum=_MAX_OPERATOR_MEMORY_BYTES,
    ),
    "compiler.groupmemory": ParamSpec(
        ParamKind.BYTES,
        "Memory budget in bytes for a group-by operator instance in a partition.",
        minimum=_MIN_OPERATOR_MEMORY_BYTES,
        maximum=_MAX_OPERATOR_MEMORY_BYTES,
    ),
    "compiler.windowmemory": ParamSpec(
        ParamKind.BYTES,
        "Memory budget in bytes for a window operator instance in a partition.",
        minimum=_MIN_OPERATOR_MEMORY_BYTES,
        maximum=_MAX_OPERATOR_MEMORY_BYTES,
    ),
    "compiler.cbo": ParamSpec(
        ParamKind.BOOLEAN,
        "Enable cost-based optimization.",
    ),
    "compiler.forcejoinorder": ParamSpec(
        ParamKind.BOOLEAN,
        "Force the join order to follow the order written in the query.",
    ),
    "compiler.index.covering": ParamSpec(
        ParamKind.BOOLEAN,
        "Enable index-only (covering index) plans.",
    ),
    "compiler.arrayindex": ParamSpec(
        ParamKind.BOOLEAN,
        "Enable use of array indexes in queries.",
    ),
    "compiler.column.filter": ParamSpec(
        ParamKind.BOOLEAN,
        "Enable use of columnar min/max filters.",
    ),
    "compiler.sort.parallel": ParamSpec(
        ParamKind.BOOLEAN,
        "Enable full parallel sort.",
    ),
}


def validate_compiler_parameters(params: dict[str, Any]) -> dict[str, str]:
    """Validate a compilerParameters object against the allowlist.

    Returns a dict of CC form-field name to canonical string value, ready to
    forward verbatim. Every value has already been range- and type-checked.

    Raises:
        GatewayError: INVALID_PARAMETER for an unknown key, wrong type, or
            out-of-range value. The message names the offending key so the LLM
            can correct a single knob without re-sending the whole object.
    """
    validated: dict[str, str] = {}
    for key, value in params.items():
        spec = ALLOWLIST.get(key)
        if spec is None:
            raise GatewayError(
                ErrorType.INVALID_PARAMETER,
                f"Unknown compiler parameter {key!r}. Allowed keys: "
                f"{', '.join(sorted(ALLOWLIST))}. See the asterixdb://config-parameters "
                f"resource for types and ranges.",
            )
        validated[key] = _validate_value(key, value, spec)
    return validated


def _validate_value(key: str, value: Any, spec: ParamSpec) -> str:
    """Validate one value against its spec; return the canonical form string."""
    if spec.kind is ParamKind.BOOLEAN:
        return _validate_boolean(key, value)
    return _validate_numeric(key, value, spec)


def _validate_boolean(key: str, value: Any) -> str:
    """Accept a real bool or the strings 'true'/'false' (case-insensitive)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower()
    raise GatewayError(
        ErrorType.INVALID_PARAMETER,
        f"Compiler parameter {key!r} expects a boolean, got {value!r}.",
    )


def _validate_numeric(key: str, value: Any, spec: ParamSpec) -> str:
    """Validate an integer/bytes value and enforce the spec's [min, max] range."""
    number = _coerce_int(key, value, spec.kind)
    if spec.minimum is not None and number < spec.minimum:
        raise GatewayError(
            ErrorType.INVALID_PARAMETER,
            f"Compiler parameter {key!r} must be >= {spec.minimum}, got {number}.",
        )
    if spec.maximum is not None and number > spec.maximum:
        raise GatewayError(
            ErrorType.INVALID_PARAMETER,
            f"Compiler parameter {key!r} must be <= {spec.maximum}, got {number}.",
        )
    return str(number)


def _coerce_int(key: str, value: Any, kind: ParamKind) -> int:
    """Coerce a value to int, rejecting bools and non-integral inputs."""
    # bool is an int subclass; a true/false here is a caller mistake, not a count.
    if isinstance(value, bool):
        raise GatewayError(
            ErrorType.INVALID_PARAMETER,
            f"Compiler parameter {key!r} expects {kind.value}, got a boolean.",
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise GatewayError(
                ErrorType.INVALID_PARAMETER,
                f"Compiler parameter {key!r} expects {kind.value}, got {value!r}.",
            ) from exc
    raise GatewayError(
        ErrorType.INVALID_PARAMETER,
        f"Compiler parameter {key!r} expects {kind.value}, got {value!r}.",
    )


def describe_allowlist() -> list[dict[str, Any]]:
    """Render the allowlist as JSON-friendly records for the config resource."""
    records: list[dict[str, Any]] = []
    for key, spec in ALLOWLIST.items():
        record: dict[str, Any] = {
            "name": key,
            "kind": spec.kind.value,
            "description": spec.description,
        }
        if spec.minimum is not None:
            record["minimum"] = spec.minimum
        if spec.maximum is not None:
            record["maximum"] = spec.maximum
        records.append(record)
    return records
