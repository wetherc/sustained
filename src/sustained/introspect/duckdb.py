"""
The DuckDB read: information_schema, plus DuckDB's own catalog
functions for indexes, constraints, comments, and enum types.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Sequence, Tuple, cast

from sustained.introspect.information_schema import (
    DUCKDB_CATALOG,
    _catalog_filter,
    _information_schema_plan,
    _merge_plain_indexes,
)
from sustained.introspect.model import (
    IntrospectedForeignKey,
    IntrospectedIndex,
    SchemaPlan,
    Snapshot,
)
from sustained.types import RowValue

_DUCKDB_IDENTIFIER_RE = re.compile(r"^\w+$")
# One part of duckdb_indexes()'s expressions list. DuckDB writes a plain
# column bare, and writes any other part as a string literal: a quoted
# column such as '"select"', or an expression such as '(lower(name))'.
_DUCKDB_INDEX_PART_RE = re.compile(r"'((?:[^']|'')*)'|[^,\s]+")
_DUCKDB_QUOTED_COLUMN_RE = re.compile(r'^"((?:[^"]|"")*)"$')


def _duckdb_index_column(part: str) -> Optional[str]:
    """
    One part of an index's expressions list as a column name, or None
    when the part is an expression.
    """
    if not part.startswith("'"):
        return part if _DUCKDB_IDENTIFIER_RE.match(part) else None
    text = part[1:-1].replace("''", "'")
    quoted = _DUCKDB_QUOTED_COLUMN_RE.match(text)
    if quoted is not None:
        return quoted.group(1).replace('""', '"')
    return text if _DUCKDB_IDENTIFIER_RE.match(text) else None


def _duckdb_index_columns(expressions: str) -> Optional[Tuple[str, ...]]:
    """
    The column list in duckdb_indexes()'s expressions field, spelled
    '[a, b]'. DuckDB quotes a column whose name is a keyword or is not a
    plain word, so a part may arrive as '"select"'. An expression index
    has parts that are not column names; it cannot be compared against a
    model's column list, so it reads as None and stays out of the schema.
    """
    body = expressions.strip()
    if body.startswith("[") and body.endswith("]"):
        body = body[1:-1]
    columns = [
        _duckdb_index_column(match.group(0))
        for match in _DUCKDB_INDEX_PART_RE.finditer(body)
    ]
    if not columns or any(column is None for column in columns):
        return None
    return tuple(cast(str, column).lower() for column in columns)


def _replace_duckdb_constraints(
    schema: Snapshot, rows: Sequence[Sequence[RowValue]]
) -> None:
    """
    Replaces the foreign keys and checks of the information_schema read
    with duckdb_constraints() rows. That view reports where a key points,
    and it reports each check once as its bare expression. The
    information_schema view reports a two-column check twice, drops a
    check that names no column, and wraps the rest in CHECK(...), which
    never compares equal to a declared expression.
    """
    foreign_keys: Dict[str, Dict[str, IntrospectedForeignKey]] = {}
    checks: Dict[str, Dict[str, str]] = {}
    for table, ctype, name, columns, target, target_columns, expression in rows:
        key = str(table).lower()
        if str(ctype) == "CHECK":
            checks.setdefault(key, {})[str(name).lower()] = str(expression)
            continue
        foreign_keys.setdefault(key, {})[str(name).lower()] = IntrospectedForeignKey(
            columns=tuple(
                str(c).lower() for c in cast(Sequence[RowValue], columns or ())
            ),
            target_table=str(target).lower(),
            target_columns=tuple(
                str(c).lower() for c in cast(Sequence[RowValue], target_columns or ())
            ),
        )
    for table, existing in list(schema.items()):
        schema[table] = existing._replace(
            foreign_keys=foreign_keys.get(table, {}),
            checks=checks.get(table, {}),
        )
    schema.constraints_read = True
    schema.checks_read = True


def _duckdb_plan(schemas: Tuple[str, ...] = ()) -> SchemaPlan:
    """
    information_schema plus duckdb_indexes(). DuckDB's shared read sees
    unique constraints only, so a model's declared index would read as
    missing on every plan, and the second run would fail creating it
    again.
    """
    schema = yield from _information_schema_plan(DUCKDB_CATALOG, schemas)
    schema_filter = _catalog_filter(DUCKDB_CATALOG, "schema_name", schemas)
    try:
        index_rows = yield (
            "SELECT table_name, index_name, is_unique, expressions "
            f"FROM duckdb_indexes() WHERE NOT is_primary "
            f"AND {schema_filter}"
        )
        plain: Dict[str, Dict[str, IntrospectedIndex]] = {}
        for table, name, is_unique, expressions in index_rows:
            columns = _duckdb_index_columns(str(expressions or ""))
            if columns is None:
                continue
            plain.setdefault(str(table).lower(), {})[str(name).lower()] = (
                IntrospectedIndex(columns, bool(is_unique), name=str(name))
            )
        _merge_plain_indexes(schema, plain)
    except Exception:
        # No duckdb_indexes() to read; keep the constraint-derived indexes.
        pass
    try:
        constraint_rows = yield (
            "SELECT table_name, constraint_type, constraint_name, "
            "constraint_column_names, referenced_table, "
            "referenced_column_names, expression FROM duckdb_constraints() "
            "WHERE constraint_type IN ('FOREIGN KEY', 'CHECK') "
            f"AND {schema_filter}"
        )
        _replace_duckdb_constraints(schema, constraint_rows)
    except Exception:
        # No duckdb_constraints() to read; keep the information_schema read.
        pass
    try:
        comment_rows = yield (
            "SELECT table_name, column_name, comment FROM duckdb_columns() "
            f"WHERE comment IS NOT NULL AND {schema_filter}"
        )
        for table, name, comment in comment_rows:
            existing = schema.get(str(table).lower())
            if existing is None:
                continue
            key = str(name).lower()
            column = existing.columns.get(key)
            if column is not None:
                existing.columns[key] = column._replace(comment=str(comment))
        schema.comments_read = True
    except Exception:
        # A DuckDB from before COMMENT ON; degrade to no comments.
        pass
    try:
        type_rows = yield (
            "SELECT type_name, labels FROM duckdb_types() "
            "WHERE labels IS NOT NULL AND NOT internal "
            f"AND {schema_filter}"
        )
        for name, labels in type_rows:
            if not labels:
                continue
            schema.enum_types[str(name).lower()] = tuple(
                str(label) for label in cast(Sequence[RowValue], labels)
            )
        schema.enum_types_read = True
    except Exception:
        # A DuckDB from before duckdb_types(); the diff falls back to
        # inferring a type's presence from the columns that use it.
        pass
    return schema
