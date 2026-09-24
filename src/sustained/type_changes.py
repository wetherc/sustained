"""
Whether a column type change can lose the data the column has.

The diff against the models changes a column's type in place with ALTER
TABLE. The engine converts each value, and a narrower type truncates,
rounds, or rejects values it cannot fit: numeric(18,6) to numeric(18,2)
drops two decimal places, double to integer drops the fraction,
timestamp to date drops the time, and MySQL text to varchar(10) cuts
each string at ten characters. The down step restores the type but not
the data, so the rehearsal's reversed check passes such a change.

`type_change_loses_data()` compares the type the catalog reports with
the type the model declares. It returns False only for a change it
recognizes as a widening, such as int to bigint or varchar(50) to
varchar(100). Every other change counts as one that can lose data,
including a change between two type families and a type spelling it
does not recognize. A change that counts as lossy asks for a rehearsal
receipt before `migrate` runs it, and a missed one would run unproven.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple, Optional, Tuple

from sustained.introspect import parse_inline_enum, type_params

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.schema import ColumnDef

# Signed integer types by width in bits.
_INTEGER_BITS = {
    "tinyint": 8,
    "smallint": 16,
    "int2": 16,
    "mediumint": 24,
    "int": 32,
    "integer": 32,
    "int4": 32,
    "bigint": 64,
    "int8": 64,
}
# The decimal digits the widest value of each integer width needs. An
# unsigned type needs the same digits, except bigint unsigned, which
# reaches 20.
_INTEGER_DIGITS = {8: 3, 16: 5, 24: 8, 32: 10, 64: 19}
_FLOAT_TYPES = {"real", "float4", "float", "float8", "double", "double precision"}
_DECIMAL_TYPES = {"numeric", "decimal"}
# Bounded strings carry their length as a type parameter.
_BOUNDED_STRING_TYPES = {
    "varchar",
    "character varying",
    "char",
    "character",
    "bpchar",
    "nvarchar",
    "nchar",
}
# MySQL's TEXT and BLOB stop at 64 KB, so its larger kinds narrow to them.
_LARGE_TEXT_TYPES = {"mediumtext", "longtext"}
_TEXT_TYPES = {"text", "tinytext", "string", "ntext"} | _LARGE_TEXT_TYPES
_LARGE_BINARY_TYPES = {"mediumblob", "longblob"}
_BINARY_TYPES = {
    "bytea",
    "blob",
    "tinyblob",
    "binary",
    "varbinary",
    "image",
} | _LARGE_BINARY_TYPES
_BOOLEAN_TYPES = {"boolean", "bool", "bit"}
_TIMESTAMP_TYPES = {"timestamp", "datetime", "datetime2", "smalldatetime"}
_TIMEZONE_TYPES = {"timestamptz", "datetimeoffset"}
_JSON_TYPES = {"json", "jsonb"}

_PARAMS_RE = re.compile(r"\(([^)]*)\)")
_MODIFIER_RE = re.compile(r"\b(?:unsigned|signed|zerofill)\b")
_ZONE_RE = re.compile(r"\bwith(?:out)? time zone\b")


class _LiveType(NamedTuple):
    """One catalog type spelling, taken apart."""

    base: str
    params: Tuple[int, ...]
    unsigned: bool
    time_zone: bool
    enum_values: Tuple[str, ...]


def _parse_live_type(raw_type: str) -> _LiveType:
    """
    Splits a catalog spelling such as 'int unsigned', 'numeric(18,6)' or
    'timestamp with time zone' into its base name and its parts. A
    parameter that is not a number, such as MSSQL's MAX, is left out.
    """
    enum_values = parse_inline_enum(raw_type)
    if enum_values:
        return _LiveType("enum", (), False, False, enum_values)
    text = raw_type.strip().lower()
    match = _PARAMS_RE.search(text)
    params: Tuple[int, ...] = ()
    if match is not None:
        parts = [part.strip() for part in match.group(1).split(",")]
        params = tuple(int(part) for part in parts if part.isdigit())
        text = text[: match.start()] + " " + text[match.end() :]
    text = " ".join(text.split())
    unsigned = bool(re.search(r"\bunsigned\b", text))
    time_zone = bool(re.search(r"\bwith time zone\b", text))
    base = " ".join(_ZONE_RE.sub(" ", _MODIFIER_RE.sub(" ", text)).split())
    if base in _TIMEZONE_TYPES:
        time_zone = True
    return _LiveType(base, params, unsigned, time_zone, ())


def _is_mysql_boolean(live: _LiveType) -> bool:
    return live.base == "tinyint" and live.params == (1,)


def _integer_bits(live: _LiveType) -> Optional[int]:
    """
    The signed width a live integer type needs, or None when the type is
    not an integer. An unsigned type needs one bit more than its width.
    MySQL's tinyint(1) is a boolean and reads as None.
    """
    bits = _INTEGER_BITS.get(live.base)
    if bits is None or _is_mysql_boolean(live):
        return None
    return bits + 1 if live.unsigned else bits


def _integer_digits(live: _LiveType) -> Optional[int]:
    """The decimal digits a live integer type needs, None for a non-integer."""
    bits = _INTEGER_BITS.get(live.base)
    if bits is None or _is_mysql_boolean(live):
        return None
    return 20 if bits == 64 and live.unsigned else _INTEGER_DIGITS[bits]


def _is_string(live: _LiveType) -> bool:
    return (
        live.base == "enum"
        or live.base in _BOUNDED_STRING_TYPES
        or live.base in _TEXT_TYPES
    )


def _fits_integer(live: _LiveType, target_bits: int) -> bool:
    bits = _integer_bits(live)
    return bits is not None and bits <= target_bits


def _fits_decimal(live: _LiveType, precision: int, scale: int) -> bool:
    digits = _integer_digits(live)
    if digits is not None:
        return precision - scale >= digits
    if live.base not in _DECIMAL_TYPES or not live.params:
        return False
    live_precision = live.params[0]
    live_scale = live.params[1] if len(live.params) > 1 else 0
    return scale >= live_scale and precision - scale >= live_precision - live_scale


def _fits_string(live: _LiveType, length: Optional[int]) -> bool:
    """
    Whether every value of a live string type fits a string type of
    `length` characters, or of any length when `length` is None. A
    bounded live type with no length in the catalog, as MSSQL reports
    it, may be longer than `length` and does not fit.
    """
    if not _is_string(live):
        return False
    if length is None:
        return live.base not in _LARGE_TEXT_TYPES
    if live.base == "enum":
        return max(len(value) for value in live.enum_values) <= length
    return (
        live.base in _BOUNDED_STRING_TYPES
        and bool(live.params)
        and live.params[0] <= length
    )


def _fits_float(live: _LiveType) -> bool:
    # A double has 53 bits of mantissa, so an integer up to 32 bits wide
    # converts exactly and a bigint does not.
    return live.base in _FLOAT_TYPES or _fits_integer(live, 32)


def _fits_timestamp(live: _LiveType) -> bool:
    # A fractional precision the catalog reports may be finer than the
    # dialect's default precision, which the model type takes.
    if live.base == "date":
        return True
    return live.base in _TIMESTAMP_TYPES and not live.time_zone and not live.params


def _fits_binary(live: _LiveType) -> bool:
    return live.base in _BINARY_TYPES and live.base not in _LARGE_BINARY_TYPES


def _declared_length(expected_rendered: str) -> int:
    """The length a check-strategy enum's VARCHAR(n) rendering declares."""
    params = type_params(expected_rendered)
    assert params is not None and params.isdigit()
    return int(params)


def type_change_loses_data(
    compiler: "Compiler",
    coldef: "ColumnDef",
    expected_rendered: str,
    raw_type: str,
) -> bool:
    """
    Whether changing a column from `raw_type`, as the catalog reports it,
    to the type `coldef` declares can lose data. `expected_rendered` is
    the model type as `compiler` renders it, which gives the length of a
    check-strategy enum column and the physical type of a JSON column on
    a dialect that stores JSON as text.
    """
    live = _parse_live_type(raw_type)
    type_name = coldef.type_name
    if type_name == "JSON" and expected_rendered.upper().startswith("NVARCHAR"):
        # MSSQL stores JSON as NVARCHAR(MAX), so the change is to text.
        type_name = "TEXT"
    if type_name == "ENUM" and compiler.enum_strategy() == "inline":
        assert coldef.enum_values is not None
        return live.base != "enum" or not set(live.enum_values) <= set(
            coldef.enum_values
        )
    if type_name == "ENUM" and compiler.enum_strategy() == "check":
        return not _fits_string(live, _declared_length(expected_rendered))
    if type_name == "INTEGER":
        return not _fits_integer(live, 32)
    if type_name == "BIGINT":
        return not _fits_integer(live, 64)
    if type_name == "NUMERIC":
        if coldef.precision is None:
            return True
        return not _fits_decimal(live, coldef.precision, coldef.scale or 0)
    if type_name == "FLOAT":
        return not _fits_float(live)
    if type_name == "VARCHAR":
        if coldef.length is None and expected_rendered.upper() == "NVARCHAR":
            # MSSQL reads NVARCHAR with no length as NVARCHAR(1).
            return True
        return not _fits_string(live, coldef.length)
    if type_name == "TEXT":
        return not _fits_string(live, None)
    if type_name == "BOOLEAN":
        return not (live.base in _BOOLEAN_TYPES or _is_mysql_boolean(live))
    if type_name == "DATE":
        return live.base != "date"
    if type_name == "TIMESTAMP":
        return not _fits_timestamp(live)
    if type_name == "BINARY":
        return not _fits_binary(live)
    if type_name == "JSON":
        return live.base not in _JSON_TYPES
    return True


def removed_enum_values(coldef: "ColumnDef", raw_type: str) -> Tuple[str, ...]:
    """
    The values of a live MySQL enum column that the model's value list
    leaves out, in the column's order. Empty when the column is not an
    inline enum or keeps every value.
    """
    declared = set(coldef.enum_values or ())
    return tuple(v for v in parse_inline_enum(raw_type) if v not in declared)
