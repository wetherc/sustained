"""
The MySQL and MariaDB read: information_schema, plus the statistics
view for plain indexes, the referential view for where each key points,
and MariaDB's json_valid checks for its JSON columns.
"""

from __future__ import annotations

import re
from typing import Dict, Generator, List, Optional, Sequence, Tuple, cast

from sustained.introspect.information_schema import (
    MYSQL_CATALOG,
    _information_schema_plan,
    _merge_plain_indexes,
    _replace_foreign_keys,
)
from sustained.introspect.model import IntrospectedIndex, IntrospectedTable, SchemaPlan
from sustained.introspect.normalize import normalize_type, parse_inline_enum
from sustained.introspect.scope import _scoped_filter
from sustained.types import RowValue

# The whole body of the CHECK constraint MariaDB writes for a JSON column.
_JSON_VALID_RE = re.compile(
    r"^\s*json_valid\(\s*`?(\w+)`?\s*\)\s*$",
    re.IGNORECASE,
)


def _mysql_plan(schemas: Tuple[str, ...] = ()) -> SchemaPlan:
    # Every query in the plan covers the same schemas. A read that took
    # its columns from one schema and its indexes from another would
    # attach an index to a table of the same name in the wrong schema.
    current = MYSQL_CATALOG.current_schema_sql
    assert current is not None
    table_filter = _scoped_filter("table_schema", current, schemas)
    constraint_filter = _scoped_filter("constraint_schema", current, schemas)
    schema = yield from _information_schema_plan(MYSQL_CATALOG, schemas)
    yield from _recover_mariadb_json(schema, constraint_filter)
    try:
        index_rows = yield (
            "SELECT table_name, index_name, non_unique, column_name "
            "FROM information_schema.statistics "
            f"WHERE {table_filter} "
            "ORDER BY table_name, index_name, seq_in_index"
        )
        parts: Dict[Tuple[str, str, bool], List[Optional[str]]] = {}
        spelled: Dict[str, str] = {}
        for table, name, non_unique, column in index_rows:
            if str(name).upper() == "PRIMARY":
                continue
            key = (str(table).lower(), str(name).lower(), not int(str(non_unique)))
            parts.setdefault(key, []).append(
                None if column is None else str(column).lower()
            )
            spelled.setdefault(str(name).lower(), str(name))
        plain: Dict[str, Dict[str, IntrospectedIndex]] = {}
        for (table, name, unique), columns in parts.items():
            if any(column is None for column in columns):
                # A functional index part has no column name; it cannot
                # be compared against a model's column list.
                continue
            plain.setdefault(table, {})[name] = IntrospectedIndex(
                tuple(cast(str, column) for column in columns),
                unique,
                name=spelled[name],
            )
        _merge_plain_indexes(schema, plain)
    except Exception:
        # No statistics view; keep the constraint-derived indexes.
        pass
    try:
        fk_rows = yield (
            "SELECT kcu.table_name, kcu.constraint_name, kcu.column_name, "
            "kcu.referenced_table_name, kcu.referenced_column_name, "
            "rc.delete_rule, rc.update_rule, "
            "NULLIF(kcu.referenced_table_schema, DATABASE()) "
            "FROM information_schema.key_column_usage kcu "
            "JOIN information_schema.referential_constraints rc "
            "ON rc.constraint_schema = kcu.constraint_schema "
            "AND rc.constraint_name = kcu.constraint_name "
            "AND rc.table_name = kcu.table_name "
            "WHERE kcu.referenced_table_name IS NOT NULL "
            f"AND {_scoped_filter('kcu.table_schema', current, schemas)} "
            "ORDER BY kcu.table_name, kcu.constraint_name, kcu.ordinal_position"
        )
        _replace_foreign_keys(schema, fk_rows)
    except Exception:
        # No referential_constraints view; keep the keys without targets.
        pass
    for table in schema.values():
        for name, column in table.columns.items():
            values = parse_inline_enum(column.raw_type)
            if values:
                # A MySQL enum lives inline on its column, so there is no
                # standalone type name to carry.
                table.columns[name] = column._replace(enum_values=values)
    return schema


def _recover_mariadb_json(
    schema: Dict[str, IntrospectedTable],
    constraint_filter: str,
) -> Generator[str, List[Sequence[RowValue]], None]:
    """
    Restores the JSON type to columns MariaDB reports as longtext.

    MariaDB's JSON is a longtext with a `json_valid` CHECK constraint on
    it, and the catalog reports the storage type, not the alias. Left
    alone, a model column declared Json() would read as drift on every
    plan, with no migration able to close it. The check constraint says
    which columns those are.
    """
    try:
        rows = yield (
            "SELECT table_name, check_clause "
            "FROM information_schema.check_constraints "
            f"WHERE {constraint_filter}"
        )
    except Exception:
        # MySQL's own check_constraints view has no table_name column, and
        # MariaDB before 10.2.22 has no such view. Neither case needs the
        # recovery: MySQL stores JSON as JSON, and older MariaDB has no
        # constraint to read.
        return
    for table, clause in rows:
        match = _JSON_VALID_RE.match(str(clause))
        if match is None:
            continue
        introspected = schema.get(str(table).lower())
        if introspected is None:
            continue
        name = match.group(1).lower()
        column = introspected.columns.get(name)
        # Only a column reading as text is promoted, so a hand-written
        # json_valid check on a real JSON column changes nothing.
        if column is None or normalize_type(column.raw_type) != "TEXT":
            continue
        introspected.columns[name] = column._replace(raw_type="JSON")
        # MariaDB writes this constraint itself for every JSON column.
        # It belongs to the column's type, so it is not a check the
        # model failed to declare.
        table_key = str(table).lower()
        remaining = {
            check: expression
            for check, expression in introspected.checks.items()
            if check != name
        }
        schema[table_key] = introspected._replace(checks=remaining)
