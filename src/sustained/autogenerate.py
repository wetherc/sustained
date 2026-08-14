"""
Schema autogeneration: diff the live database against model tableColumns
declarations and produce a Migration.

diff_schema() introspects the database and reports missing tables, new
columns, extra tables and columns, and changed columns. autogenerate()
turns the additive part of that diff into a Migration with matching down
steps, so the change is fully reversible: CREATE TABLE reverses with DROP
TABLE, ADD COLUMN reverses with DROP COLUMN.

Destructive changes never happen silently. Dropping extra tables and
columns requires allow_drops=True, and those steps carry no down step
because the dropped data cannot be restored. Changed column types and
nullability are reported but never migrated automatically: SQLite cannot
alter a column's type in place, and a type rewrite is exactly the change a
human should review. autogenerate() raises when the diff contains changes
it will not express, unless told to ignore them.

Only column presence, type, and nullability are compared. Constraint
changes such as primary keys, unique indexes, and foreign keys are out of
scope for diffing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, Tuple, Type

from sustained.dialects import Dialects
from sustained.migrations import Migration
from sustained.schema import build_create_table_sql, render_column_sql

if TYPE_CHECKING:
    from sustained.model import Model
    from sustained.schema import ColumnDef


class IntrospectedColumn(NamedTuple):
    """One column as reported by the database."""

    raw_type: str
    nullable: bool
    primary_key: bool


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
}

_TYPE_PARAMS_RE = re.compile(r"\s*\(.*\)\s*$")


def normalize_type(raw: str) -> str:
    """
    Reduces an engine type spelling to a logical type name, dropping length
    and precision parameters. Unknown spellings return uppercased as-is.
    """
    base = _TYPE_PARAMS_RE.sub("", raw).strip().upper()
    return _TYPE_SYNONYMS.get(base, base)


class SchemaDiff:
    """The differences between declared models and the live database."""

    def __init__(self) -> None:
        self.missing_tables: List[Type["Model"]] = []
        self.new_columns: List[Tuple[Type["Model"], str, "ColumnDef"]] = []
        self.extra_tables: List[str] = []
        self.extra_columns: List[Tuple[str, str]] = []
        self.changed_columns: List[Tuple[str, str, str, str]] = []

    def is_empty(self) -> bool:
        return not (
            self.missing_tables
            or self.new_columns
            or self.extra_tables
            or self.extra_columns
            or self.changed_columns
        )

    def summary(self) -> str:
        """A human-readable description of every difference."""
        lines: List[str] = []
        for model in self.missing_tables:
            lines.append(f"create table {model.tableName}")
        for model, name, _ in self.new_columns:
            lines.append(f"add column {model.tableName}.{name}")
        for table in self.extra_tables:
            lines.append(f"drop table {table} (destructive)")
        for table, name in self.extra_columns:
            lines.append(f"drop column {table}.{name} (destructive)")
        for table, name, actual, expected in self.changed_columns:
            lines.append(
                f"change column {table}.{name}: database has {actual}, "
                f"model declares {expected} (not auto-migrated)"
            )
        return "\n".join(lines) if lines else "schema up to date"


def introspect_schema(
    connection: Any, dialect: Dialects = Dialects.DEFAULT
) -> Dict[str, Dict[str, IntrospectedColumn]]:
    """
    Reads tables and columns from the database. The default dialect reads
    SQLite's PRAGMA tables; every other dialect reads
    information_schema.columns. Table and column names are keyed lowercase.
    """
    cursor = connection.cursor()
    schema: Dict[str, Dict[str, IntrospectedColumn]] = {}

    if dialect == Dialects.DEFAULT:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            cursor.execute(f"PRAGMA table_info({table})")
            columns = {}
            for _, name, raw_type, notnull, _, pk in cursor.fetchall():
                columns[name.lower()] = IntrospectedColumn(
                    raw_type=raw_type or "",
                    nullable=not notnull,
                    primary_key=bool(pk),
                )
            schema[table.lower()] = columns
        return schema

    cursor.execute(
        "SELECT table_name, column_name, data_type, is_nullable "
        "FROM information_schema.columns ORDER BY table_name, ordinal_position"
    )
    for table, name, data_type, is_nullable in cursor.fetchall():
        table_columns = schema.setdefault(table.lower(), {})
        table_columns[name.lower()] = IntrospectedColumn(
            raw_type=data_type or "",
            nullable=str(is_nullable).upper() == "YES",
            primary_key=False,
        )
    return schema


def diff_schema(
    connection: Any,
    models: List[Type["Model"]],
    dialect: Dialects = Dialects.DEFAULT,
    exclude_tables: Tuple[str, ...] = ("sustained_migrations",),
) -> SchemaDiff:
    """
    Compares the models' tableColumns declarations against the live
    database and returns the differences.
    """
    compiler = Dialects.get_compiler(dialect)
    diff = SchemaDiff()

    declared: Dict[str, Type["Model"]] = {}
    for model in models:
        if not model.tableName or not model.tableColumns:
            raise ValueError(
                f"Model '{model.__name__}' needs tableName and tableColumns "
                "to participate in schema diffing."
            )
        key = model.tableName.lower()
        if key in declared:
            raise ValueError(f"Two models declare the table '{model.tableName}'.")
        declared[key] = model

    excluded = {t.lower() for t in exclude_tables}
    actual = introspect_schema(connection, dialect)

    for table_key, model in declared.items():
        assert model.tableColumns is not None
        actual_columns = actual.get(table_key)
        if actual_columns is None:
            diff.missing_tables.append(model)
            continue
        for name, coldef in model.tableColumns.items():
            actual_col = actual_columns.get(name.lower())
            if actual_col is None:
                diff.new_columns.append((model, name, coldef))
                continue
            expected_type = normalize_type(compiler.compile_column_type(coldef))
            actual_type = normalize_type(actual_col.raw_type)
            type_changed = expected_type != actual_type
            # SQLite reports INTEGER PRIMARY KEY as nullable, so nullability
            # is only compared on non-key columns.
            null_changed = (
                not coldef.primary_key
                and not actual_col.primary_key
                and actual_col.nullable != coldef.nullable
            )
            if type_changed or null_changed:
                expected_desc = expected_type + ("" if coldef.nullable else " NOT NULL")
                actual_desc = actual_type + ("" if actual_col.nullable else " NOT NULL")
                diff.changed_columns.append(
                    (model.tableName or "", name, actual_desc, expected_desc)
                )
        declared_names = {c.lower() for c in model.tableColumns}
        for name in actual_columns:
            if name not in declared_names:
                diff.extra_columns.append((model.tableName or "", name))

    for table_key in actual:
        if table_key not in declared and table_key not in excluded:
            diff.extra_tables.append(table_key)

    return diff


def autogenerate(
    connection: Any,
    models: List[Type["Model"]],
    id: str,
    dialect: Dialects = Dialects.DEFAULT,
    allow_drops: bool = False,
    ignore_changed_columns: bool = False,
    exclude_tables: Tuple[str, ...] = ("sustained_migrations",),
) -> Optional[Migration]:
    """
    Diffs the database against the models and builds a Migration for the
    differences. Returns None when the schema is up to date.

    Missing tables and new columns generate reversible steps. Extra tables
    and columns generate drops only with allow_drops=True, and the
    migration then has no down step, since dropped data cannot come back.
    Changed columns raise unless ignore_changed_columns=True, because type
    and nullability rewrites need a hand-written migration.
    """
    compiler = Dialects.get_compiler(dialect)
    diff = diff_schema(connection, models, dialect, exclude_tables)

    if diff.changed_columns and not ignore_changed_columns:
        details = "; ".join(
            f"{table}.{name}: {actual} -> {expected}"
            for table, name, actual, expected in diff.changed_columns
        )
        raise ValueError(
            "Changed columns need a hand-written migration: "
            f"{details}. Pass ignore_changed_columns=True to skip them."
        )
    if (diff.extra_tables or diff.extra_columns) and not allow_drops:
        dropped = [t for t in diff.extra_tables] + [
            f"{t}.{c}" for t, c in diff.extra_columns
        ]
        raise ValueError(
            "The database has objects the models do not declare: "
            f"{', '.join(dropped)}. Pass allow_drops=True to generate the "
            "drops, or add them to exclude_tables."
        )

    up_steps: List[str] = []
    down_steps: List[str] = []
    reversible = True

    for model in diff.missing_tables:
        assert model.tableColumns is not None
        table_sql = model._qualified_table_sql()
        up_steps.append(build_create_table_sql(compiler, table_sql, model.tableColumns))
        down_steps.insert(0, model.drop_table_sql())

    for model, name, coldef in diff.new_columns:
        if coldef.primary_key or coldef.autoincrement:
            raise ValueError(
                f"Cannot add '{model.tableName}.{name}' with ALTER TABLE: "
                "primary key and autoincrement columns need a hand-written "
                "migration."
            )
        if not coldef.nullable and coldef.default is None:
            raise ValueError(
                f"Cannot add NOT NULL column '{model.tableName}.{name}' "
                "without a default; existing rows would have no value."
            )
        table_sql = model._qualified_table_sql()
        column_sql = render_column_sql(compiler, name, coldef, inline_pk=False)
        up_steps.append(compiler.compile_add_column(table_sql, column_sql))
        down_steps.insert(0, compiler.compile_drop_column(table_sql, name))

    if allow_drops:
        for table, name in diff.extra_columns:
            table_sql = compiler.quote_fully_qualified_identifier(table)
            up_steps.append(compiler.compile_drop_column(table_sql, name))
            reversible = False
        for table in diff.extra_tables:
            table_sql = compiler.quote_fully_qualified_identifier(table)
            up_steps.append(f"DROP TABLE {table_sql}")
            reversible = False

    if not up_steps:
        return None
    return Migration(
        id=id, up=up_steps, down=down_steps if reversible and down_steps else None
    )
