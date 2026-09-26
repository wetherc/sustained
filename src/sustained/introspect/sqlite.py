"""
The SQLite read: PRAGMA tables, plus the checks, constraint names,
collations, and triggers held in the SQL stored in sqlite_master.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple, cast

from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    SchemaPlan,
    Snapshot,
)
from sustained.introspect.normalize import _balanced_paren_body
from sustained.types import RowValue


def _strip_identifier(name: str) -> str:
    """An identifier with its quoting characters removed, lowercased."""
    return name.strip().strip('"`[]').lower()


# The quote that closes each quoting character SQLite takes: a string
# literal, and the three ways to quote an identifier.
_SQLITE_QUOTES = {"'": "'", '"': '"', "`": "`", "[": "]"}
# One constraint or column name as SQLite takes it: quoted any of three
# ways, or bare.
_SQLITE_NAME = r"(\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]|\w+)"
# A named constraint in a CREATE TABLE statement. Checks are read by
# name, and _sqlite_unnamed_checks() reads a CHECK written without a
# CONSTRAINT name. Foreign keys match their pragma rows by column list.
_SQLITE_CHECK_RE = re.compile(
    rf"CONSTRAINT\s+{_SQLITE_NAME}\s+CHECK\s*\(",
    re.IGNORECASE,
)
_SQLITE_FK_NAME_RE = re.compile(
    rf"CONSTRAINT\s+{_SQLITE_NAME}\s+FOREIGN\s+KEY\s*\(([^)]*)\)",
    re.IGNORECASE,
)


def _sqlite_unquote(name: str) -> str:
    """A name from a CREATE TABLE statement, unquoted and lowercased."""
    closer = _SQLITE_QUOTES.get(name[0])
    if closer is not None and name.endswith(closer):
        name = name[1:-1].replace(closer * 2, closer)
    return name.lower()


def _sqlite_table_checks(create_sql: str) -> Dict[str, str]:
    """
    The named check constraints in a CREATE TABLE statement. SQLite has
    no catalog view for checks, so they are read back out of the stored
    CREATE TABLE SQL. A CHECK with no CONSTRAINT name has no name to key
    it by, and _sqlite_unnamed_checks() reads it instead.
    """
    checks: Dict[str, str] = {}
    for match in _SQLITE_CHECK_RE.finditer(create_sql):
        body = _balanced_paren_body(create_sql, match.end() - 1)
        if body is not None:
            checks[_sqlite_unquote(match.group(1))] = body.strip()
    return checks


def _sqlite_fk_names(create_sql: str) -> Dict[Tuple[str, ...], str]:
    """Named FOREIGN KEY clauses, keyed by their column tuple."""
    names: Dict[Tuple[str, ...], str] = {}
    for match in _SQLITE_FK_NAME_RE.finditer(create_sql):
        columns = tuple(
            _strip_identifier(part)
            for part in match.group(2).split(",")
            if part.strip()
        )
        names[columns] = _sqlite_unquote(match.group(1))
    return names


def _unquoted(text: str) -> List[Tuple[int, str]]:
    """
    Every character of `text` that sits outside a string literal and a
    quoted identifier, with its position. A doubled quote closes the
    span and opens it again, which leaves it inside.
    """
    found: List[Tuple[int, str]] = []
    closer: Optional[str] = None
    for position, char in enumerate(text):
        if closer is not None:
            if char == closer:
                closer = None
        elif char in _SQLITE_QUOTES:
            closer = _SQLITE_QUOTES[char]
        else:
            found.append((position, char))
    return found


def _sqlite_table_parts(create_sql: str) -> List[str]:
    """
    The column definitions and table constraints of a CREATE TABLE
    statement, split at the commas between them. A comma inside
    parentheses or quotes belongs to its part.
    """
    parts: List[str] = []
    depth = 0
    start = 0
    for position, char in _unquoted(create_sql):
        if char == "(":
            depth += 1
            if depth == 1:
                start = position + 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                parts.append(create_sql[start:position])
                break
        elif char == "," and depth == 1:
            parts.append(create_sql[start:position])
            start = position + 1
    return [part.strip() for part in parts if part.strip()]


# The words that open a table constraint rather than a column definition.
_SQLITE_TABLE_CONSTRAINT_RE = re.compile(
    r"^(?:CONSTRAINT|PRIMARY|UNIQUE|CHECK|FOREIGN)\b", re.IGNORECASE
)
_SQLITE_COLLATE_RE = re.compile(
    r"\bCOLLATE\s+(\"(?:[^\"]|\"\")+\"|`[^`]+`|\[[^\]]+\]|\w+)", re.IGNORECASE
)
_SQLITE_CHECK_START_RE = re.compile(r"\bCHECK\s*\(", re.IGNORECASE)
# A CONSTRAINT name right before a CHECK, which makes that check named.
_SQLITE_NAMED_BEFORE_RE = re.compile(
    rf"\bCONSTRAINT\s+{_SQLITE_NAME}\s*$", re.IGNORECASE
)


def _sqlite_column_name(part: str) -> str:
    """The column a column definition names, unquoted and lowercased."""
    match = re.match(_SQLITE_NAME, part)
    return _sqlite_unquote(match.group(1) if match else part.split()[0])


def _sqlite_collations(create_sql: str) -> Dict[str, str]:
    """
    The collating sequence each column of a CREATE TABLE statement
    names, keyed by lowercased column name. PRAGMA table_info does not
    report it, so it is read from the stored statement.
    """
    collations: Dict[str, str] = {}
    for part in _sqlite_table_parts(create_sql):
        if _SQLITE_TABLE_CONSTRAINT_RE.match(part):
            continue
        match = _SQLITE_COLLATE_RE.search(part)
        if match is not None:
            collations[_sqlite_column_name(part)] = match.group(1)
    return collations


def _sqlite_unnamed_checks(create_sql: str) -> Tuple[str, ...]:
    """
    The expressions of the CHECK constraints a CREATE TABLE statement
    writes without a CONSTRAINT name, in the order they appear. They
    have no name to diff by, so a rebuild carries them as they are.
    """
    unquoted = {position for position, _ in _unquoted(create_sql)}
    found: List[str] = []
    for match in _SQLITE_CHECK_START_RE.finditer(create_sql):
        if match.start() not in unquoted:
            continue
        if _SQLITE_NAMED_BEFORE_RE.search(create_sql[: match.start()]):
            continue
        body = _balanced_paren_body(create_sql, match.end() - 1)
        if body is not None:
            found.append(body.strip())
    return tuple(found)


def _sqlite_quote(name: str) -> str:
    """
    Quotes a name for a SQLite PRAGMA.

    A table or index name can legally hold a space or a double quote, and a
    PRAGMA takes the name as SQL, not as a parameter. The name is wrapped in
    double quotes with any inner quote doubled.
    """
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _sqlite_plan() -> SchemaPlan:
    rows = yield (
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'trigger', 'view') "
        "AND name NOT LIKE 'sqlite_%'"
    )
    tables = [(str(row[1]), str(row[3] or "")) for row in rows if row[0] == "table"]
    triggers: Dict[str, List[str]] = {}
    for kind, _, table_name, sql in rows:
        if kind == "trigger" and sql:
            triggers.setdefault(str(table_name).lower(), []).append(str(sql))
    schema = Snapshot(
        constraints_read=True,
        checks_read=True,
        views=[str(row[1]) for row in rows if row[0] == "view"],
    )
    for table, create_sql in tables:
        columns: Dict[str, IntrospectedColumn] = {}
        primary_key: List[str] = []
        collations = _sqlite_collations(create_sql)
        for _, name, raw_type, notnull, default, pk in (
            yield f"PRAGMA table_info({_sqlite_quote(table)})"
        ):
            columns[name.lower()] = IntrospectedColumn(
                raw_type=raw_type or "",
                nullable=not notnull,
                primary_key=bool(pk),
                default=default,
                collation=collations.get(name.lower()),
                name=name,
            )
            if pk:
                primary_key.append(name.lower())

        fk_rows = sorted(
            (yield f"PRAGMA foreign_key_list({_sqlite_quote(table)})"),
            key=lambda row: (row[0], row[1]),
        )
        rows_by_key: Dict[int, List[Sequence[RowValue]]] = {}
        for row in fk_rows:
            rows_by_key.setdefault(int(cast(int, row[0])), []).append(row)

        declared_fk_names = _sqlite_fk_names(create_sql)
        foreign_keys: Dict[str, IntrospectedForeignKey] = {}
        for fk_id, key_rows in rows_by_key.items():
            first = key_rows[0]
            key_columns = tuple(str(r[3]).lower() for r in key_rows)
            name = declared_fk_names.get(key_columns, f"fk_{table.lower()}_{fk_id}")
            foreign_keys[name] = IntrospectedForeignKey(
                columns=key_columns,
                target_table=str(first[2]).lower(),
                target_columns=tuple(
                    str(r[4]).lower() for r in key_rows if r[4] is not None
                ),
                on_delete=None if first[6] is None else str(first[6]),
                on_update=None if first[5] is None else str(first[5]),
            )

        indexes: Dict[str, IntrospectedIndex] = {}
        index_rows = yield f"PRAGMA index_list({_sqlite_quote(table)})"
        for row in index_rows:
            index_name, unique, origin = row[1], bool(row[2]), row[3]
            if origin == "pk":
                continue
            info = yield f"PRAGMA index_info({_sqlite_quote(index_name)})"
            names = [r[2] for r in info]
            if any(name is None for name in names):
                # An expression index reports NULL column names. It cannot
                # be compared against a model's column list, so it is left
                # out of the schema rather than crashing the read.
                continue
            index_columns = tuple(name.lower() for name in names)
            indexes[index_name.lower()] = IntrospectedIndex(
                index_columns, unique, constraint=origin == "u", name=index_name
            )

        schema[table.lower()] = IntrospectedTable(
            columns=columns,
            primary_key=tuple(primary_key),
            foreign_keys=foreign_keys,
            indexes=indexes,
            checks=_sqlite_table_checks(create_sql),
            unnamed_checks=_sqlite_unnamed_checks(create_sql),
            triggers=tuple(triggers.get(table.lower(), ())),
            name=table,
        )
    return schema
