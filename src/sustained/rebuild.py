"""
The table rebuild that carries a schema change SQLite cannot make with
ALTER TABLE.

SQLite cannot change a column's type or nullability, or add and drop a
table constraint, on a table that exists. Its own recipe creates a new
table from the declaration, copies the rows across, drops the old
table, and renames the new one into its place. rebuild_steps() writes
that recipe for one model, and the autogenerate module decides which
tables need it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Dict, List, Set, Tuple, Type

from sustained.introspect import IntrospectedColumn, IntrospectedTable, Snapshot
from sustained.schema import bare_table_name, build_create_table_sql
from sustained.types import Expression

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.model import Model
    from sustained.schema import ColumnDef


def implied_constraint_names(
    compiler: "Compiler", model: Type["Model"]
) -> Tuple[Set[str], Set[Tuple[str, ...]]]:
    """
    The constraint names and foreign key column tuples a model's columns
    imply on their own: the ck_<table>_<column>_enum checks that hold
    enum columns on check-strategy dialects, and the single-column
    foreign keys of the references shorthand. Those belong to the
    columns, so they are not extras and not missing tableConstraints.
    """
    table = bare_table_name(model.tableName or "")
    check_names: Set[str] = set()
    fk_columns: Set[Tuple[str, ...]] = set()
    for name, coldef in (model.tableColumns or {}).items():
        if coldef.type_name == "ENUM" and compiler.enum_strategy() == "check":
            check_names.add(f"ck_{table}_{name}_enum".lower())
        if coldef.references is not None:
            fk_columns.add((name.lower(),))
    return check_names, fk_columns


# A default SQLite takes in ADD COLUMN: a number, a string or blob
# literal, NULL, TRUE, or FALSE. CURRENT_TIMESTAMP and an expression in
# parentheses are refused there.
_CONSTANT_DEFAULT_RE = re.compile(
    r"^\s*(?:[-+]?\d+(?:\.\d*)?(?:e[-+]?\d+)?|'(?:[^']|'')*'|x'[0-9a-f]*'"
    r"|null|true|false)\s*$",
    re.IGNORECASE,
)


def add_column_needs_rebuild(compiler: "Compiler", coldef: "ColumnDef") -> bool:
    """
    Whether SQLite refuses to add the column with ALTER TABLE ADD COLUMN,
    so the table is rebuilt to take it. SQLite refuses a UNIQUE column, a
    NOT NULL column with no default, a default that is not a constant,
    and a REFERENCES column with a default while foreign keys are on. An
    enum column needs its CHECK constraint, which SQLite cannot add to a
    table that exists. A key or identity column is refused on every
    dialect before this is asked, so it answers False.
    """
    if coldef.primary_key or coldef.autoincrement:
        return False
    if coldef.type_name == "ENUM" and compiler.enum_strategy() == "check":
        return True
    if coldef.unique or (not coldef.nullable and coldef.default is None):
        return True
    if coldef.default is None:
        return False
    if coldef.references is not None:
        return True
    return isinstance(coldef.default, Expression) and not _CONSTANT_DEFAULT_RE.match(
        str(coldef.default)
    )


def create_indexes_sql(compiler: "Compiler", model: Type["Model"]) -> List[str]:
    """
    The CREATE INDEX statements for a model's declared indexes, rendered
    for the dialect a migration is generated for rather than the one the
    model is bound to.
    """
    table_sql = model._qualified_table_sql(compiler)
    return [
        compiler.compile_create_index(
            index.name, table_sql, list(index.columns), index.unique
        )
        for index in model.indexes or []
    ]


def rebuild_steps(
    compiler: "Compiler",
    model: Type["Model"],
    actual_table: IntrospectedTable,
    allow_drops: bool = False,
) -> List[str]:
    """
    Rebuilds a SQLite table to match the model: create a new table from the
    declaration, copy rows across, replace the old table, and recreate the
    indexes. Columns, indexes, and constraints the model does not declare
    survive the rebuild unless allow_drops is True; a drop is never a
    side effect of a column change. With allow_drops they go with the old
    table, so the generator emits no separate drop for them.
    """
    assert model.tableColumns is not None and model.tableName is not None
    table = model.tableName
    table_sql = compiler.quote_ddl_identifier(table)
    temp_sql = compiler.quote_ddl_identifier(f"{table}_sustained_new")
    declared = {name.lower() for name in model.tableColumns}
    undeclared: Dict[str, IntrospectedColumn] = (
        {}
        if allow_drops
        else {
            name: col
            for name, col in actual_table.columns.items()
            if name not in declared
        }
    )
    unique_undeclared = {
        index.columns[0]
        for name, index in actual_table.indexes.items()
        if index.unique
        and len(index.columns) == 1
        and name.startswith("sqlite_autoindex")
    }
    extras = [
        _introspected_column_sql(compiler, name, col, unique=name in unique_undeclared)
        for name, col in undeclared.items()
    ]
    if not allow_drops:
        extras.extend(_carried_constraint_sql(compiler, model, actual_table))
    steps = [
        build_create_table_sql(
            compiler,
            temp_sql,
            model.tableColumns,
            extras=extras,
            constraints=model.tableConstraints,
        )
    ]

    select_parts: List[str] = []
    insert_columns: List[str] = []
    for name, coldef in model.tableColumns.items():
        name_sql = compiler.quote_ddl_identifier(name)
        insert_columns.append(name_sql)
        actual_col = actual_table.columns.get(name.lower())
        filler = coldef.backfill if coldef.backfill is not None else coldef.default
        if actual_col is None:
            select_parts.append(
                "NULL" if filler is None else compiler.format_value(filler)
            )
        elif _tightens(coldef, actual_col):
            # The copy would put each NULL into a NOT NULL column. The
            # ALTER path fills them the same way before it tightens.
            if filler is None:
                raise ValueError(
                    f"Tightening '{table}.{name}' to NOT NULL needs a "
                    "backfill or default value for existing NULLs."
                )
            select_parts.append(
                f"COALESCE({name_sql}, {compiler.format_value(filler)})"
            )
        else:
            select_parts.append(name_sql)
    for name in undeclared:
        insert_columns.append(compiler.quote_ddl_identifier(name))
        select_parts.append(compiler.quote_ddl_identifier(name))

    steps.append(
        f"INSERT INTO {temp_sql} ({', '.join(insert_columns)}) "
        f"SELECT {', '.join(select_parts)} FROM {table_sql}"
    )
    steps.append(f"DROP TABLE {table_sql}")
    steps.append(compiler.compile_rename_table(temp_sql, table_sql))
    steps.extend(create_indexes_sql(compiler, model))
    if not allow_drops:
        steps.extend(_undeclared_index_sql(compiler, table_sql, model, actual_table))
    return steps


def _tightens(coldef: "ColumnDef", actual_col: IntrospectedColumn) -> bool:
    """
    Whether the model makes a nullable column NOT NULL. SQLite reports an
    INTEGER PRIMARY KEY as nullable, so a key column never counts.
    """
    return (
        actual_col.nullable
        and not coldef.nullable
        and not coldef.primary_key
        and not actual_col.primary_key
    )


def _carried_constraint_sql(
    compiler: "Compiler",
    model: Type["Model"],
    actual_table: IntrospectedTable,
) -> List[str]:
    """
    Constraints the model does not declare, rendered back into CREATE
    TABLE parts so a rebuild carries them across. Declared constraints
    render from the declaration; the ones a column implies (the enum
    check, the references shorthand) render with the column. A foreign
    key whose target the catalog did not report cannot be re-rendered
    and is left behind.
    """
    declared_names = {c.name.lower() for c in model.tableConstraints or []}
    implied_checks, implied_fk_columns = implied_constraint_names(compiler, model)
    fragments: List[str] = []
    for name, expression in actual_table.checks.items():
        if name in declared_names or name in implied_checks:
            continue
        fragments.append(
            f"CONSTRAINT {compiler.quote_ddl_identifier(name)} CHECK ({expression})"
        )
    for name, fk in actual_table.foreign_keys.items():
        if (
            name in declared_names
            or fk.columns in implied_fk_columns
            or fk.target_table == "?"
        ):
            continue
        columns_sql = ", ".join(compiler.quote_ddl_identifier(c) for c in fk.columns)
        target_sql = compiler.quote_fully_qualified_ddl_identifier(fk.target_table)
        sql = (
            f"CONSTRAINT {compiler.quote_ddl_identifier(name)} "
            f"FOREIGN KEY ({columns_sql}) REFERENCES {target_sql}"
        )
        if fk.target_columns:
            targets_sql = ", ".join(
                compiler.quote_ddl_identifier(c) for c in fk.target_columns
            )
            sql += f" ({targets_sql})"
        if fk.on_delete is not None and fk.on_delete.upper() != "NO ACTION":
            sql += f" ON DELETE {fk.on_delete.upper()}"
        if fk.on_update is not None and fk.on_update.upper() != "NO ACTION":
            sql += f" ON UPDATE {fk.on_update.upper()}"
        fragments.append(sql)
    return fragments


def _introspected_column_sql(
    compiler: "Compiler", name: str, col: IntrospectedColumn, unique: bool = False
) -> str:
    """Renders an introspected column back into a CREATE TABLE part."""
    parts = [compiler.quote_ddl_identifier(name)]
    if col.raw_type:
        parts.append(col.raw_type)
    if not col.nullable:
        parts.append("NOT NULL")
    if unique:
        parts.append("UNIQUE")
    if col.default is not None:
        parts.append(f"DEFAULT {col.default}")
    return " ".join(parts)


def _undeclared_index_sql(
    compiler: "Compiler",
    table_sql: str,
    model: Type["Model"],
    actual_table: IntrospectedTable,
) -> List[str]:
    """
    CREATE INDEX statements for the table's indexes that the model does not
    declare, so a rebuild does not quietly discard them. SQLite's automatic
    indexes are skipped: the column constraints that made them recreate
    them.
    """
    declared_indexes = {i.name.lower() for i in model.indexes or []}
    return [
        compiler.compile_create_index(
            name, table_sql, list(index.columns), index.unique
        )
        for name, index in actual_table.indexes.items()
        if name not in declared_indexes and not name.startswith("sqlite_autoindex")
    ]


def rebuild_turns_foreign_keys_off(
    snapshot: Snapshot, rebuild_tables: Dict[str, Type["Model"]]
) -> bool:
    """
    Whether a rebuild has to turn foreign key enforcement off.

    A rebuild drops the old table, and SQLite refuses that while rows in
    another table point at it. Enforcement goes off for the drop when
    some foreign key targets a table being rebuilt, and the migration
    then runs outside a transaction, since SQLite ignores the pragma
    inside one. A rebuild nothing points at takes no pragma, so a failure
    cannot leave enforcement off. A key whose target the catalog did not
    name counts as a reference.
    """
    for table in snapshot.values():
        for fk in table.foreign_keys.values():
            target = fk.target_table.lower()
            if target == "?" or target in rebuild_tables:
                return True
    return False
