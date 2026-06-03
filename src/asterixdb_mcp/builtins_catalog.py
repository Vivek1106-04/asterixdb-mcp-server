"""Curated catalog of common AsterixDB SQL++ built-in functions.

AsterixDB ships ~800 builtins defined in ``BuiltinFunctions.java``. Generating the
full catalog is a build-time step against that source; until that codegen lands,
this is a curated, high-frequency subset covering the functions an LLM reaches
for most — with the aggregates spelled out precisely so the model stops
hallucinating ``STDEV``/``STDDEV`` (see statement_guard).

Each entry is language INTERNAL. UDFs (SQL++, Java, Python) are read live from the
Metadata catalog and merged in by list_functions/get_function; this static set is
only the built-in half.
"""

from __future__ import annotations

from dataclasses import dataclass

LANGUAGE_INTERNAL = "INTERNAL"


@dataclass(frozen=True)
class BuiltinFunction:
    """One built-in function: its name, category, and a one-line summary."""

    name: str
    category: str
    summary: str


# Curated subset. Names are the canonical AsterixDB spellings.
_BUILTINS: tuple[BuiltinFunction, ...] = (
    # Aggregates (SQL-standard). These are the names the model gets wrong most.
    BuiltinFunction("count", "aggregate", "Count of non-null items in a group."),
    BuiltinFunction("sum", "aggregate", "Sum of numeric items."),
    BuiltinFunction("avg", "aggregate", "Arithmetic mean of numeric items."),
    BuiltinFunction("min", "aggregate", "Minimum item."),
    BuiltinFunction("max", "aggregate", "Maximum item."),
    BuiltinFunction("stddev_samp", "aggregate", "Sample standard deviation."),
    BuiltinFunction("stddev_pop", "aggregate", "Population standard deviation."),
    BuiltinFunction("var_samp", "aggregate", "Sample variance."),
    BuiltinFunction("var_pop", "aggregate", "Population variance."),
    BuiltinFunction("array_agg", "aggregate", "Collect group items into an array."),
    # String
    BuiltinFunction("length", "string", "Length of a string."),
    BuiltinFunction("lowercase", "string", "Lowercase a string."),
    BuiltinFunction("uppercase", "string", "Uppercase a string."),
    BuiltinFunction("substr", "string", "Substring by position/length."),
    BuiltinFunction("trim", "string", "Trim surrounding whitespace/characters."),
    BuiltinFunction("contains", "string", "Whether a string contains a substring."),
    BuiltinFunction("string_concat", "string", "Concatenate an array of strings."),
    BuiltinFunction("split", "string", "Split a string into an array on a separator."),
    BuiltinFunction("regexp_contains", "string", "Whether a string matches a regex."),
    # Numeric
    BuiltinFunction("abs", "numeric", "Absolute value."),
    BuiltinFunction("ceil", "numeric", "Ceiling."),
    BuiltinFunction("floor", "numeric", "Floor."),
    BuiltinFunction("round", "numeric", "Round to nearest."),
    BuiltinFunction("sqrt", "numeric", "Square root."),
    BuiltinFunction("power", "numeric", "Raise to a power."),
    # Date / time
    BuiltinFunction("current_datetime", "datetime", "Current datetime."),
    BuiltinFunction("datetime", "datetime", "Construct a datetime from a string."),
    BuiltinFunction("get_year", "datetime", "Year component of a date/datetime."),
    BuiltinFunction("get_month", "datetime", "Month component of a date/datetime."),
    BuiltinFunction("get_day", "datetime", "Day component of a date/datetime."),
    # Array
    BuiltinFunction("array_length", "array", "Length of an array."),
    BuiltinFunction("array_sort", "array", "Sort an array."),
    BuiltinFunction("array_distinct", "array", "Distinct items of an array."),
    BuiltinFunction("array_contains", "array", "Whether an array contains a value."),
    # Object
    BuiltinFunction("object_names", "object", "Field names of an object."),
    BuiltinFunction("object_values", "object", "Field values of an object."),
    BuiltinFunction("object_remove", "object", "Object without a named field."),
    # Type / conditional
    BuiltinFunction("is_null", "type", "Whether a value is null."),
    BuiltinFunction("is_missing", "type", "Whether a value is missing."),
    BuiltinFunction("is_unknown", "type", "Whether a value is null or missing."),
    BuiltinFunction("coalesce", "conditional", "First non-null/non-missing argument."),
    BuiltinFunction("if_null", "conditional", "Second argument when the first is null."),
    BuiltinFunction("to_string", "type", "Cast a value to string."),
    BuiltinFunction("to_number", "type", "Cast a value to number."),
)

BUILTINS_BY_NAME: dict[str, BuiltinFunction] = {fn.name: fn for fn in _BUILTINS}


def all_builtins() -> tuple[BuiltinFunction, ...]:
    """Return the curated built-in functions."""
    return _BUILTINS
