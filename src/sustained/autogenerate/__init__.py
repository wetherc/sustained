"""
Schema autogeneration: diff the live database against model declarations
and produce a Migration.

diff_schema() reads the live schema through sustained.introspect and
compares it with the models' tableColumns and indexes declarations.
autogenerate() turns the diff into a Migration:

- Missing tables and columns, index changes, and renames generate
  reversible steps.
- Type and nullability changes generate ALTER COLUMN on dialects that
  support it (Postgres, MSSQL, DuckDB) with a reversing down step. On
  SQLite, which cannot alter columns in place, the table is rebuilt:
  a new table is created from the model, rows are copied across, and the
  old table is replaced. Rebuilds are not reversible.
- Renames cannot be detected from the catalog, so they are operator
  hints: renames={'table.old': 'new'} and table_renames={'old': 'new'}
  emit RENAME statements and stop the columns from diffing as drop+add.
- NOT NULL columns added to or tightened on populated tables need a
  default or a backfill value on the ColumnDef; generation emits
  add-nullable, UPDATE backfill, SET NOT NULL where needed.
- Dropping extra tables or columns requires allow_drops=True and is not
  reversible. On Postgres and DuckDB, a named enum type that only the
  dropped tables and columns used, and that no model declares, is
  dropped after them. Dropping extra indexes also requires allow_drops=True but
  reverses, since the index definition is known.
- Declared tableConstraints diff by name on engines whose catalog reports
  constraints, and by content on DuckDB, which renames them. DuckDB
  cannot change a constraint on a table that exists, so a difference
  there stays a note. A missing constraint generates ADD CONSTRAINT with
  the drop as its down step; a changed foreign key generates drop-plus-add
  under allow_drops; SQLite routes constraint changes through the table
  rebuild. A changed check expression on an engine that rewrites
  expressions stays a note, never a drop.
- Primary key, column-shorthand foreign key, newly declared
  column-level unique, and default differences are reported in the
  diff's constraint notes but never auto-migrated. A UNIQUE constraint
  a column no longer declares is an extra object, dropped under
  allow_drops.
- Column comments diff on engines whose catalog reported them, and a
  drifted comment generates the engine's comment statement with the old
  comment written back on the way down. A degraded comment read diffs
  no comments: an absent value there is not proof of absence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Type

from sustained.analysis import MigrationStatement, with_intent
from sustained.autogenerate.column_steps import (
    _add_column_rebuild_scan,
    _changed_column_steps,
    _comment_steps,
    _enum_checks_off,
    _enum_checks_on,
    _lift_index_steps,
    _new_column_steps,
    _restore_lifted_index_steps,
)
from sustained.autogenerate.constraints import (
    _Actual,
    _bare_reference,
    _Declared,
    _diff_constraints,
    _diff_declared_constraints,
    _diff_enum_checks,
    _enum_check_values,
    _fk_action,
    _fk_matches,
    _fk_target_matches,
    _named,
    _pair_constraints,
    _PairTest,
    _same_check,
    _same_fk,
    _same_fk_columns,
)
from sustained.autogenerate.diff import (
    SchemaDiff,
    _actual_column_is_enum,
    _apply_renames,
    _column_type_changed,
    _comment_or_none,
    _constraints_fixed_at_create,
    _declared_enum_types,
    _declared_enum_values,
    _dependency_order,
    _diff_columns,
    _diff_enum_types,
    _diff_indexes,
    _enum_value_additions,
    _foreign_key_targets,
    _ordered_missing_tables,
    _orphaned_enum_types,
    _rename_in_expression,
)
from sustained.autogenerate.statements import (
    _add_enum_check,
    _add_foreign_key,
    _can_probe_rows,
    _create_table_steps,
    _declared_fk_intent,
    _declared_fk_sql,
    _declared_table_sql,
    _deferred_foreign_key_steps,
    _extra_table_drops,
    _foreign_keys_setting,
    _index_intent,
    _intent_table,
    _introspected_fk_sql,
    _introspected_state,
    _lift_statements,
    _lifted_indexes,
    _preserving_state,
    _rebuild_needed,
    _refuse_enum_value_removal,
    _relaxed_copy,
    _reported_intent_table,
    _snapshot_table_sql,
    _spelled_columns,
    _table_has_rows,
    _tagged,
)
from sustained.autogenerate.steps import (
    _column_rename_steps,
    _constraint_rebuild_scan,
    _constraint_steps,
    _created_enum_type_downs,
    _drop_steps,
    _enum_type_steps,
    _Generation,
    _index_steps,
    _new_table_steps,
    _refuse_undeclared,
    _table_rebuild_steps,
    _table_rename_steps,
)
from sustained.compilers.base import table_qualifier
from sustained.dialects import Dialects
from sustained.exceptions import DialectError
from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
    async_introspect_schema,
    diff_snapshots,
    introspect_schema,
    is_sequence_default,
    normalize_check,
    normalize_default,
    normalize_type,
    parse_inline_enum,
    type_params,
)
from sustained.migrations import Migration, _ReplayConnection
from sustained.rebuild import (
    add_column_needs_rebuild,
    create_indexes_sql,
    implied_constraint_names,
    rebuild_renames_under_legacy,
    rebuild_steps,
    rebuild_turns_foreign_keys_off,
)
from sustained.schema import (
    Check,
    ColumnState,
    ForeignKey,
    bare_table_name,
    build_create_table_sql,
    checked_constraint_names,
    collect_enum_types,
    enum_check_constraint_sql,
    render_column_sql,
)
from sustained.type_changes import removed_enum_values, type_change_loses_data
from sustained.types import Connection

if TYPE_CHECKING:
    from sustained.model import Model

# Reading a schema moved to sustained.introspect, and the helpers of
# this module to the modules of this package. Every name the module
# defined or imported stays importable from here, where callers have
# always found them.
__all__ = [
    "IntrospectedColumn",
    "IntrospectedForeignKey",
    "IntrospectedIndex",
    "IntrospectedTable",
    "SchemaDiff",
    "Snapshot",
    "async_introspect_schema",
    "autogenerate",
    "diff_schema",
    "diff_snapshots",
    "introspect_schema",
    "normalize_default",
    "normalize_type",
]


def declared_schemas(models: List[Type["Model"]]) -> Tuple[str, ...]:
    """
    The schemas the models name in tableSchema, sorted and without
    repeats. A read covers these on top of the schema the connection is
    on, so a model outside the connection's own schema still diffs.
    """
    return tuple(sorted({m.tableSchema for m in models if m.tableSchema}))


def diff_schema(
    connection: Connection,
    models: List[Type["Model"]],
    dialect: Dialects = Dialects.DEFAULT,
    exclude_tables: Tuple[str, ...] = ("sustained_migrations",),
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    snapshot: Optional[Snapshot] = None,
) -> SchemaDiff:
    """
    Compares the models' declarations against the live database and
    returns the differences. Rename hints are applied first, so renamed
    objects compare under their new names.

    Args:
        snapshot: A schema already read with introspect_schema(), used
            instead of reading the database again. The rename hints are
            applied to it in place, so the caller sees the same renamed
            schema this function compares against. The connection is not
            touched when a snapshot is passed.
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
            first = declared[key]
            raise ValueError(
                f"Two models declare the table '{model.tableName}': "
                f"'{first.__name__}' in schema "
                f"{first.tableSchema or 'the connection default'} and "
                f"'{model.__name__}' in schema "
                f"{model.tableSchema or 'the connection default'}. A schema "
                "read keys on the bare table name, so the two cannot be "
                "told apart. Diff them in separate calls."
            )
        checked_constraint_names(model.tableName, model.tableConstraints)
        declared[key] = model

    excluded = {t.lower() for t in exclude_tables}
    actual = (
        introspect_schema(connection, dialect, declared_schemas(models))
        if snapshot is None
        else snapshot
    )
    _apply_renames(actual, renames or {}, table_renames or {})

    declared_types = _declared_enum_types(models)
    if compiler.enum_strategy() == "native":
        _diff_enum_types(diff, declared, declared_types, actual)

    for table_key, model in declared.items():
        assert model.tableColumns is not None
        actual_table = actual.get(table_key)
        if actual_table is None:
            diff.missing_tables.append(model)
            continue
        _diff_columns(compiler, diff, model, actual_table, actual)
        _diff_indexes(compiler, diff, model, actual_table)
        _diff_constraints(compiler, diff, model, actual_table, actual)

    if diff.missing_tables:
        cycle_notes: List[str] = []
        diff.missing_tables = _ordered_missing_tables(diff.missing_tables, cycle_notes)
        # A dialect that can add a constraint later never needs the
        # order, so a cycle there is not worth reporting.
        if not compiler.supports_add_constraint():
            diff.constraint_notes.extend(cycle_notes)

    for table_key in actual:
        if table_key not in declared and table_key not in excluded:
            diff.extra_tables.append(actual[table_key].name or table_key)

    if compiler.enum_strategy() == "native" and actual.enum_types_read:
        diff.extra_enum_types = _orphaned_enum_types(
            actual, diff.extra_tables, diff.extra_columns, declared_types
        )

    return diff


def autogenerate(
    connection: Connection,
    models: List[Type["Model"]],
    id: str,
    dialect: Dialects = Dialects.DEFAULT,
    allow_drops: bool = False,
    ignore_changed_columns: bool = False,
    exclude_tables: Tuple[str, ...] = ("sustained_migrations",),
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    type_casts: Optional[Dict[str, str]] = None,
    ignore_undeclared: bool = False,
    snapshot: Optional[Snapshot] = None,
) -> Optional[Migration]:
    """
    Diffs the database against the models and builds a Migration for the
    differences. Returns None when the schema is up to date.

    Args:
        renames: Column rename hints, {'table.old_name': 'new_name'}.
        table_renames: Table rename hints, {'old_name': 'new_name'}.
        type_casts: Postgres USING expressions for type changes,
            {'table.column': 'expression'}.
        allow_drops: Also drop extra tables, columns, and indexes.
        ignore_changed_columns: Skip type and nullability changes instead
            of migrating them.
        ignore_undeclared: Leave objects the models do not declare alone
            instead of refusing to generate. A database managed partly by
            hand-written migrations holds tables no model declares, and
            those are not a reason to stop. A CHECK constraint no model
            declares never refuses: engines rewrite a check expression on
            the way in, so a check the models do write can read as one
            they do not. It comes back as a note on the diff instead.
        snapshot: A schema already read with introspect_schema(), used
            instead of reading it again. The function works on a copy,
            so one read can feed several calls with different options.
            The connection still answers the row checks that decide
            whether a table is empty.
    """
    compiler = Dialects.get_compiler(dialect)
    renames = renames or {}
    table_renames = table_renames or {}
    type_casts = type_casts or {}
    # One read of the live schema for both the diff and the steps below.
    # diff_schema() applies the rename hints to it in place, so a snapshot
    # from the caller is copied first.
    actual = (
        introspect_schema(connection, dialect, declared_schemas(models))
        if snapshot is None
        else snapshot.copy()
    )
    diff = diff_schema(
        connection,
        models,
        dialect,
        exclude_tables,
        renames,
        table_renames,
        snapshot=actual,
    )
    models_by_table = {m.tableName.lower(): m for m in models if m.tableName}

    _refuse_undeclared(diff, allow_drops, ignore_undeclared)

    state = _Generation(
        connection,
        compiler,
        diff,
        actual,
        models_by_table,
        allow_drops,
        ignore_changed_columns,
        type_casts,
    )
    # Renames first, so later steps address the new names.
    _table_rename_steps(state, table_renames)
    _column_rename_steps(state, renames)
    _enum_type_steps(state)
    _new_table_steps(state)
    _enum_checks_off(state)
    _lift_index_steps(state)
    _changed_column_steps(state)
    _enum_checks_on(state)
    _add_column_rebuild_scan(state)
    _new_column_steps(state)
    _restore_lifted_index_steps(state)
    _comment_steps(state)
    _constraint_rebuild_scan(state)
    _table_rebuild_steps(state)
    _index_steps(state)
    _constraint_steps(state)
    _drop_steps(state)
    _created_enum_type_downs(state)

    if not state.up_steps:
        return None
    return Migration(
        id=id,
        up=state.up_steps,
        down=state.down_steps if state.reversible and state.down_steps else None,
        transactional=state.transactional,
    )
