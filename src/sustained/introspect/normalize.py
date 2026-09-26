"""
Reducing reported type, check, and default spellings to forms that
compare equal across engines, and writing MySQL's catalog spellings
back as SQL.
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Tuple

# Engine type spellings mapped to Sustained's logical types. Both sides of
# a comparison pass through this table, so a model column compared against
# the table its own DDL created always matches.
_TYPE_SYNONYMS = {
    "INT": "INTEGER",
    "INT4": "INTEGER",
    "INTEGER": "INTEGER",
    "BIGINT": "BIGINT",
    "INT8": "BIGINT",
    "VARCHAR": "VARCHAR",
    "CHARACTER VARYING": "VARCHAR",
    "NVARCHAR": "VARCHAR",
    "TEXT": "TEXT",
    "STRING": "TEXT",
    # MySQL sizes its text columns in the type name. TINYINT is left off
    # this table on purpose: TINYINT(1) is how MySQL spells a boolean, and
    # folding plain TINYINT into INTEGER would make the two the same
    # column to a diff.
    "TINYTEXT": "TEXT",
    "MEDIUMTEXT": "TEXT",
    "LONGTEXT": "TEXT",
    "BOOLEAN": "BOOLEAN",
    "BOOL": "BOOLEAN",
    "BIT": "BOOLEAN",
    "FLOAT": "FLOAT",
    "FLOAT8": "FLOAT",
    "DOUBLE": "FLOAT",
    "DOUBLE PRECISION": "FLOAT",
    "REAL": "FLOAT",
    "NUMERIC": "NUMERIC",
    "DECIMAL": "NUMERIC",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "TIMESTAMP WITHOUT TIME ZONE": "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
    "DATETIME": "TIMESTAMP",
    "DATETIME2": "TIMESTAMP",
    "JSON": "JSON",
    "JSONB": "JSON",
    # Binary columns. MySQL sizes them in the type name the way it sizes
    # text; Postgres calls the type bytea; MSSQL reports varbinary.
    "BYTEA": "BINARY",
    "BLOB": "BINARY",
    "TINYBLOB": "BINARY",
    "MEDIUMBLOB": "BINARY",
    "LONGBLOB": "BINARY",
    "VARBINARY": "BINARY",
    "BINARY VARYING": "BINARY",
}

_TYPE_PARAMS_RE = re.compile(r"\s*\((.*)\)\s*$")


def normalize_type(raw: str) -> str:
    """
    Reduces an engine type spelling to a logical type name, dropping length
    and precision parameters. Unknown spellings return uppercased as-is.
    """
    base = _TYPE_PARAMS_RE.sub("", raw).strip().upper()
    return _TYPE_SYNONYMS.get(base, base)


def type_params(raw: str) -> Optional[str]:
    """Extracts '(120)' style parameters from a type spelling, normalized."""
    match = _TYPE_PARAMS_RE.search(raw)
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(1)).upper()


# The parts of an expression a normalization must not touch: a string
# literal keeps its spaces and its quoting characters.
_SQL_LITERAL_RE = re.compile(r"('(?:[^']|'')*')")
# An identifier the engine quoted on the way in: "col", `col`, [col].
_QUOTED_IDENTIFIER_RE = re.compile(r'"([^"]*)"|`([^`]*)`|\[([^\]]*)\]')
# The spacing an engine puts around an operator, which it rewrites to
# its own taste: MSSQL reports [price]>(0) for price > 0.
_OPERATOR_SPACING_RE = re.compile(r"\s*([<>=!+\-*/%,])\s*")
# Parentheses around one word, which MSSQL writes around every literal.
# The open paren must not follow an identifier character, or the pattern
# would take the parentheses off a call and fold LENGTH(name) to
# lengthname. Two different expressions would then read as the same one.
_LONE_PARENS_RE = re.compile(r"(?<![\w)])\((\w+)\)")


def _outside_literals(value: str, change: Callable[[str], str]) -> str:
    """`change` applied to every part of `value` outside a string literal."""
    parts = _SQL_LITERAL_RE.split(value)
    return "".join(
        part if index % 2 else change(part) for index, part in enumerate(parts)
    )


def _unquote_identifiers(text: str) -> str:
    """The text with the quoting characters off its identifiers."""
    return _QUOTED_IDENTIFIER_RE.sub(
        lambda match: next(group for group in match.groups() if group is not None),
        text,
    )


def normalize_check(expression: str) -> str:
    """
    Reduces a check expression to a comparable form: whitespace collapsed,
    identifier quoting removed, operator spacing and parentheses around a
    single word taken off, balanced outer parentheses stripped, and
    casefolded. String literals keep their spelling. A call keeps its
    parentheses, so LENGTH(name) > 5 stays a call.

    The rewriting is what an engine does to a check on the way in. MySQL
    and MariaDB report `price` > 0 for the expression price > 0, and
    MSSQL reports ([price]>(0)). A model declares the bare expression, so
    without this the two would never compare equal and the difference
    would stand as a note no migration can close. Engines rewrite
    expressions further than this repairs, so two spellings that compare
    equal are the same check, while a mismatch is only a doubt.
    """
    value = re.sub(r"\s+", " ", expression).strip()
    value = _outside_literals(value, _unquote_identifiers)
    value = _outside_literals(value, lambda part: _OPERATOR_SPACING_RE.sub(r"\1", part))
    value = _outside_literals(value, lambda part: _LONE_PARENS_RE.sub(r"\1", part))
    while (
        value.startswith("(")
        and value.endswith(")")
        and _balanced_paren_body(value, 0) == value[1:-1]
    ):
        value = value[1:-1].strip()
    return value.casefold()


# A Postgres ::type cast, with the length or precision it may carry and
# any array brackets, so 'x'::character varying(255) reduces to 'x'.
_CAST_RE = re.compile(
    r"::\s*[a-zA-Z_][a-zA-Z_0-9 ]*(?:\s*\(\s*[\d,\s]*\))?(?:\s*\[\s*\])*"
)

_SEQUENCE_DEFAULT_RE = re.compile(r"^\s*nextval\s*\(", re.IGNORECASE)


def is_sequence_default(raw: Optional[str]) -> bool:
    """
    Whether a reported default is a call to a sequence, which is how
    Postgres spells a serial column's default. A model has no way to
    declare one, so there is nothing to compare it against.
    """
    return raw is not None and bool(_SEQUENCE_DEFAULT_RE.match(str(raw)))


def normalize_default(raw: Optional[str]) -> Optional[str]:
    """
    Reduces a reported column default to a comparable form: strips
    balanced outer parentheses, Postgres ::type casts, the N prefix of an
    MSSQL Unicode string, quotes, and an empty argument list, and
    uppercases. The argument list is why
    MariaDB's current_timestamp() and MySQL's CURRENT_TIMESTAMP compare
    equal.

    A sequence call reduces to None. It is what Postgres reports for a
    serial column, and no model declaration can ever equal it.

    Parentheses come off only when they balance, so an expression such
    as (1)+(2) keeps its shape. The cast comes off before the quotes,
    since the cast may carry a length that the quote strip would leave
    behind.
    """
    if raw is None:
        return None
    if is_sequence_default(raw):
        return None
    value = str(raw).strip()
    while (
        value.startswith("(")
        and value.endswith(")")
        and _balanced_paren_body(value, 0) == value[1:-1]
    ):
        value = value[1:-1].strip()
    value = _CAST_RE.sub("", value)
    # MSSQL reports a Unicode string default as N'...'.
    if value[:2] in ("N'", "n'"):
        value = value[1:]
    value = value.strip("'\"")
    value = re.sub(r"\(\s*\)$", "", value.strip())
    return value.upper()


def _balanced_paren_body(text: str, start: int) -> Optional[str]:
    """
    The text between the parenthesis at `start` and its matching close,
    or None when the parentheses do not balance. Quoted strings are
    skipped, so a ')' inside a literal does not end the expression.
    """
    depth = 0
    in_string = False
    for position in range(start, len(text)):
        char = text[position]
        if in_string:
            if char == "'":
                in_string = False
            continue
        if char == "'":
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : position]
    return None


_MYSQL_ENUM_RE = re.compile(r"^\s*enum\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_MYSQL_ENUM_VALUE_RE = re.compile(r"'((?:[^']|'')*)'")


# Types whose MySQL default the catalog reports in a form that is
# already SQL: a number, a bit literal b'101', or a hex literal 0x6162.
_MYSQL_BARE_DEFAULT_TYPES_RE = re.compile(
    r"^\s*(?:(?:tiny|small|medium|big)?int|integer|decimal|numeric|float"
    r"|double|real|bit|binary|varbinary)\b",
    re.IGNORECASE,
)
_MYSQL_TIME_TYPES_RE = re.compile(r"^\s*(?:datetime|timestamp)\b", re.IGNORECASE)
_MYSQL_NOW_RE = re.compile(
    r"^\s*(?:current_timestamp|now|localtime|localtimestamp)"
    r"(?:\s*\(\s*\d*\s*\))?\s*$",
    re.IGNORECASE,
)


def mysql_default_sql(default: str, extra: str, raw_type: str) -> str:
    """
    A MySQL catalog default written as SQL for a DEFAULT clause.

    MySQL 8 reports a string literal without its quotes, so 'raw' comes
    back as raw and '' as an empty string. An expression default comes
    back without its parentheses and with DEFAULT_GENERATED in the EXTRA
    column, so (uuid()) comes back as uuid(). MySQL refuses either one
    restated as reported. CURRENT_TIMESTAMP on a datetime or timestamp
    column needs no parentheses, and MySQL 5.7 reports it without the
    DEFAULT_GENERATED mark.
    """
    generated = "DEFAULT_GENERATED" in extra.upper()
    if _MYSQL_NOW_RE.match(default) and (
        generated or _MYSQL_TIME_TYPES_RE.match(raw_type)
    ):
        return default
    if generated:
        return f"({default})"
    if _MYSQL_BARE_DEFAULT_TYPES_RE.match(raw_type):
        return default
    escaped = default.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


def parse_inline_enum(raw_type: str) -> Tuple[str, ...]:
    """
    The values of a MySQL enum('a','b') column type, empty when the type
    is not an enum. A quote inside a value arrives doubled and is put
    back to one.
    """
    match = _MYSQL_ENUM_RE.match(raw_type)
    if match is None:
        return ()
    return tuple(
        value.replace("''", "'")
        for value in _MYSQL_ENUM_VALUE_RE.findall(match.group(1))
    )
