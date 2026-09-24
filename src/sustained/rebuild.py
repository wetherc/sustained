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

from typing import TYPE_CHECKING, Dict, List, Set, Tuple, Type

from sustained.introspect import IntrospectedColumn, IntrospectedTable, Snapshot
from sustained.schema import bare_table_name, build_create_table_sql

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.model import Model


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


def rebuild_steps(
    compiler: "Compiler",
    model: Type["Model"],
    actual_table: IntrospectedTable,
    allow_drops: bool = False,
) -> List[str]:
    """
    Rebuilds a SQLite table to match the model: create a new table from the
    declaration, copy rows across, replace the old table, and recreate the
    indexes. Columns and indexes the model does not declare survive the
    rebuild unless allow_drops is True; a drop is never a side effect of a
    column change.
    """
    assert model.tableColumns is not None and model.tableName is not None
    table = model.tableName
    temp = f"{table}_sustained_new"
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
        _introspected_column_sql(name, col, unique=name in unique_undeclared)
        for name, col in undeclared.items()
    ]
    if not allow_drops:
        extras.extend(_carried_constraint_sql(compiler, model, actual_table))
    steps = [
        build_create_table_sql(
            compiler,
            temp,
            model.tableColumns,
            extras=extras,
            constraints=model.tableConstraints,
        )
    ]

    select_parts: List[str] = []
    insert_columns: List[str] = []
    for name, coldef in model.tableColumns.items():
        insert_columns.append(name)
        exists = name.lower() in actual_table.columns
        if exists and not coldef.nullable and coldef.backfill is not None:
            filler = compiler.format_value(coldef.backfill)
            select_parts.append(f"COALESCE({name}, {filler})")
        elif exists:
            select_parts.append(name)
        elif coldef.backfill is not None:
            select_parts.append(compiler.format_value(coldef.backfill))
        elif coldef.default is not None:
            select_parts.append(compiler.format_value(coldef.default))
        else:
            select_parts.append("NULL")
    for name in undeclared:
        insert_columns.append(name)
        select_parts.append(name)

    steps.append(
        f"INSERT INTO {temp} ({', '.join(insert_columns)}) "
        f"SELECT {', '.join(select_parts)} FROM {table}"
    )
    steps.append(f"DROP TABLE {table}")
    steps.append(compiler.compile_rename_table(temp, table))
    steps.extend(model.create_indexes_sql())
    steps.extend(
        _undeclared_index_sql(
            compiler, table, model, actual_table, declared, undeclared
        )
    )
    return steps


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
    name: str, col: IntrospectedColumn, unique: bool = False
) -> str:
    """Renders an introspected column back into a CREATE TABLE part."""
    parts = [name]
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
    table: str,
    model: Type["Model"],
    actual_table: IntrospectedTable,
    declared_columns: Set[str],
    undeclared_columns: Dict[str, IntrospectedColumn],
) -> List[str]:
    """
    CREATE INDEX statements for the table's indexes that the model does not
    declare, so a rebuild does not quietly discard them. SQLite's automatic
    indexes are skipped: the column constraints that made them recreate
    them. An index on a column the rebuild dropped is skipped too.
    """
    declared_indexes = {i.name.lower() for i in model.indexes or []}
    surviving = declared_columns | set(undeclared_columns)
    statements: List[str] = []
    for name, index in actual_table.indexes.items():
        if name in declared_indexes or name.startswith("sqlite_autoindex"):
            continue
        if not all(column in surviving for column in index.columns):
            continue
        table_sql = compiler.quote_fully_qualified_ddl_identifier(table)
        statements.append(
            compiler.compile_create_index(
                name, table_sql, list(index.columns), index.unique
            )
        )
    return statements


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
