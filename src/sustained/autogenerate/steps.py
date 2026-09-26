"""
The state autogenerate() threads through its phases, and the phases
that work on whole tables: renames, enum types, new tables, rebuilds,
indexes, constraints, and drops. The column phases are in
sustained.autogenerate.column_steps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Tuple, Type

from sustained.analysis import with_intent
from sustained.autogenerate.diff import SchemaDiff, _enum_value_additions
from sustained.autogenerate.statements import (
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
    _rebuild_needed,
    _reported_intent_table,
    _spelled_columns,
    _tagged,
)
from sustained.compilers.base import table_qualifier
from sustained.introspect import Snapshot
from sustained.rebuild import (
    rebuild_renames_under_legacy,
    rebuild_steps,
    rebuild_turns_foreign_keys_off,
)
from sustained.schema import ColumnState
from sustained.types import Connection

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.model import Model


class _Generation:
    """
    What autogenerate() threads through its phases: the inputs every
    phase reads, the up and down steps they append to in order, and
    what one phase leaves for a later one.
    """

    def __init__(
        self,
        connection: Connection,
        compiler: "Compiler",
        diff: SchemaDiff,
        actual: Snapshot,
        models_by_table: Dict[str, Type["Model"]],
        allow_drops: bool,
        ignore_changed_columns: bool,
        type_casts: Dict[str, str],
    ) -> None:
        self.connection = connection
        self.compiler = compiler
        self.diff = diff
        self.actual = actual
        self.models_by_table = models_by_table
        self.allow_drops = allow_drops
        self.ignore_changed_columns = ignore_changed_columns
        self.type_casts = type_casts
        self.up_steps: List[str] = []
        self.down_steps: List[str] = []
        self.reversible = True
        self.transactional = True
        self.rebuild_tables: Dict[str, Type["Model"]] = {}
        # Enum types this migration creates, dropped last on the way down.
        self.created_enum_types: List[str] = []
        # Enum columns whose check comes off before the column changes
        # and goes back on after them.
        self.enum_check_adds: List[Tuple[Type["Model"], str]] = []
        # Indexes dropped before the column changes and created again
        # after the new columns are in.
        self.lift_drops: List[str] = []
        self.lift_creates: List[str] = []
        # The state each changed column is left in by its type and
        # nullability statements, for a comment statement that restates
        # the whole column after them.
        self.restated_states: Dict[Tuple[str, str], ColumnState] = {}


def _refuse_undeclared(
    diff: SchemaDiff, allow_drops: bool, ignore_undeclared: bool
) -> None:
    """Refuses a diff with objects the models do not declare, unless told."""
    if (
        (
            diff.extra_tables
            or diff.extra_columns
            or diff.extra_indexes
            or diff.extra_foreign_keys
        )
        and not allow_drops
        and not ignore_undeclared
    ):
        dropped = (
            list(diff.extra_tables)
            + [f"{t}.{c}" for t, c in diff.extra_columns]
            + [f"index {n}" for _, n, _ in diff.extra_indexes]
            + [f"foreign key {n}" for _, n, _ in diff.extra_foreign_keys]
        )
        raise ValueError(
            "The database has objects the models do not declare: "
            f"{', '.join(dropped)}. Pass allow_drops=True to generate the "
            "drops, or add them to exclude_tables."
        )


def _table_rename_steps(state: _Generation, table_renames: Dict[str, str]) -> None:
    """RENAME TABLE for each table rename hint."""
    compiler = state.compiler
    actual = state.actual
    models_by_table = state.models_by_table
    up_steps = state.up_steps
    down_steps = state.down_steps
    for old, new in table_renames.items():
        # The renamed table keeps its schema, so the old name takes the
        # schema the model declares for the new one.
        new_sql = _declared_table_sql(compiler, models_by_table, actual, new)
        old_sql = table_qualifier(new_sql) + compiler.quote_ddl_identifier(old)
        new_model = models_by_table.get(new.lower())
        schema = None if new_model is None else new_model.tableSchema
        up_steps.append(
            with_intent(
                compiler.compile_rename_table(old_sql, new_sql),
                "rename_table",
                f"{schema}.{old}" if schema else old,
                new=new,
            )
        )
        down_steps.insert(0, compiler.compile_rename_table(new_sql, old_sql))


def _column_rename_steps(state: _Generation, renames: Dict[str, str]) -> None:
    """RENAME COLUMN for each column rename hint."""
    compiler = state.compiler
    actual = state.actual
    models_by_table = state.models_by_table
    up_steps = state.up_steps
    down_steps = state.down_steps
    for path, new_name in renames.items():
        table, old_name = path.rsplit(".", 1)
        table_sql = _declared_table_sql(compiler, models_by_table, actual, table)
        up_steps.append(
            with_intent(
                compiler.compile_rename_column(table_sql, old_name, new_name),
                "rename_column",
                _reported_intent_table(models_by_table, actual, table),
                old_name,
                new=new_name,
            )
        )
        down_steps.insert(
            0, compiler.compile_rename_column(table_sql, new_name, old_name)
        )


def _enum_type_steps(state: _Generation) -> None:
    """Creates new enum types and appends declared values."""
    compiler = state.compiler
    diff = state.diff
    up_steps = state.up_steps
    # Enum types first, before any table or column that references them.
    # New types are created; declared values that extend the database's
    # list are appended with ADD VALUE, which no engine takes back, so
    # such a migration has no down. Any other value change cannot run in
    # place and refuses with the recipe.
    for type_name, values in diff.new_enum_types:
        up_steps.append(
            with_intent(
                compiler.compile_create_enum_type(type_name, list(values)),
                "create_enum_type",
                None,
                name=type_name,
            )
        )
        state.created_enum_types.append(type_name)
    for type_name, actual_values, expected_values in diff.changed_enum_types:
        additions = _enum_value_additions(actual_values, expected_values)
        if additions is None:
            raise ValueError(
                f"Enum '{type_name}' has values removed or reordered: the "
                f"database has ({', '.join(actual_values)}), the models "
                f"declare ({', '.join(expected_values)}). The engine "
                "cannot do that in place. Write a migration that creates "
                "a new type, converts each column with ALTER COLUMN ... "
                "USING, and drops the old type."
            )
        for value in additions:
            up_steps.append(
                with_intent(
                    compiler.compile_add_enum_value(type_name, value),
                    "add_enum_value",
                    None,
                    name=type_name,
                    value=value,
                )
            )
        state.reversible = False


def _new_table_steps(state: _Generation) -> None:
    """CREATE TABLE for each missing table."""
    compiler = state.compiler
    diff = state.diff
    up_steps = state.up_steps
    down_steps = state.down_steps
    # New tables. Where the dialect can add a constraint to a table that
    # already exists, every foreign key is left out of CREATE TABLE and
    # added afterwards, so two new tables may point at each other in any
    # order. Where it cannot, the tables were sorted into dependency
    # order by the diff and the keys stay inside CREATE TABLE.
    defer_foreign_keys = compiler.supports_add_constraint()
    table_downs: List[str] = []
    fk_downs: List[str] = []
    for model in diff.missing_tables:
        up_steps.extend(_create_table_steps(compiler, model, defer_foreign_keys))
        table_downs.insert(
            0, f"DROP TABLE IF EXISTS {model._qualified_table_sql(compiler)}"
        )
    if defer_foreign_keys:
        for model in diff.missing_tables:
            for add_sql, drop_sql in _deferred_foreign_key_steps(compiler, model):
                up_steps.append(add_sql)
                fk_downs.insert(0, drop_sql)
    down_steps[0:0] = fk_downs + table_downs


def _constraint_rebuild_scan(state: _Generation) -> None:
    """Marks the tables whose constraints change for a rebuild."""
    compiler = state.compiler
    diff = state.diff
    models_by_table = state.models_by_table
    allow_drops = state.allow_drops
    rebuild_tables = state.rebuild_tables
    # A dialect that cannot alter a table in place takes its constraint
    # changes through the rebuild: the rebuilt CREATE TABLE renders the
    # declared tableConstraints. Extra and changed constraints only
    # trigger a rebuild under allow_drops, since replacing the table
    # drops what the declaration does not carry.
    if not compiler.supports_alter_column():
        constrained_tables: List[Type["Model"]] = [
            model for model, _ in diff.new_foreign_keys
        ]
        constrained_tables += [model for model, _ in diff.new_checks]
        constrained_tables += [model for model, _, _ in diff.changed_checks]
        if allow_drops:
            constrained_tables += [model for model, _, _ in diff.changed_foreign_keys]
            constrained_tables += [
                models_by_table[table.lower()]
                for table, _, _ in diff.extra_foreign_keys
            ]
            constrained_tables += [
                models_by_table[table.lower()] for table, _, _ in diff.extra_checks
            ]
            constrained_tables += [
                models_by_table[table.lower()]
                for table, _, index in diff.extra_indexes
                if index.constraint
            ]
        for model in constrained_tables:
            if _rebuild_needed(compiler, "change a constraint"):
                rebuild_tables[(model.tableName or "").lower()] = model


def _table_rebuild_steps(state: _Generation) -> None:
    """Rebuilds each table marked for a rebuild."""
    compiler = state.compiler
    actual = state.actual
    allow_drops = state.allow_drops
    up_steps = state.up_steps
    rebuild_tables = state.rebuild_tables
    # Table rebuilds for SQLite consume every remaining change on the
    # table. They run between the statements that turn foreign key
    # enforcement off and on again, since dropping the old table would
    # otherwise fail while rows in another table still point at it.
    if rebuild_tables:
        # The pragma statements only land outside a transaction, so a
        # migration that carries them runs bare. A rebuild nothing points
        # at needs no pragma and keeps its transaction.
        guarded = rebuild_turns_foreign_keys_off(actual, rebuild_tables)
        if guarded:
            up_steps.extend(_foreign_keys_setting(compiler.rebuild_setup_sql(), "OFF"))
        legacy_rename = rebuild_renames_under_legacy(actual)
        for table_key, model in rebuild_tables.items():
            up_steps.extend(
                _tagged(
                    rebuild_steps(
                        compiler, model, actual[table_key], allow_drops, legacy_rename
                    ),
                    "rebuild_table",
                    _intent_table(model),
                )
            )
        if guarded:
            up_steps.extend(_foreign_keys_setting(compiler.rebuild_finish_sql(), "ON"))
            state.transactional = False
        state.reversible = False


def _index_steps(state: _Generation) -> None:
    """Creates and rebuilds declared indexes."""
    compiler = state.compiler
    diff = state.diff
    actual = state.actual
    up_steps = state.up_steps
    down_steps = state.down_steps
    rebuild_tables = state.rebuild_tables
    # Index changes. A rebuilt table takes its declared indexes from the
    # rebuild, and its old indexes went with the old table, so none of
    # these statements apply to it.
    for model, index in diff.new_indexes:
        if (model.tableName or "").lower() in rebuild_tables:
            continue
        table_sql = model._qualified_table_sql(compiler)
        up_steps.append(
            _index_intent(
                compiler.compile_create_index(
                    index.name, table_sql, list(index.columns), index.unique
                ),
                _intent_table(model),
                index,
            )
        )
        down_steps.insert(0, compiler.compile_drop_index(index.name, table_sql))
    for model, index, actual_index in diff.changed_indexes:
        if (model.tableName or "").lower() in rebuild_tables:
            continue
        table_sql = model._qualified_table_sql(compiler)
        intent_table = _intent_table(model)
        up_steps.append(
            with_intent(
                compiler.compile_drop_index(index.name, table_sql),
                "drop_index",
                intent_table,
                name=index.name,
            )
        )
        up_steps.append(
            _index_intent(
                compiler.compile_create_index(
                    index.name, table_sql, list(index.columns), index.unique
                ),
                intent_table,
                index,
            )
        )
        down_steps.insert(
            0,
            compiler.compile_create_index(
                index.name,
                table_sql,
                _spelled_columns(actual[(model.tableName or "").lower()], actual_index),
                actual_index.unique,
            ),
        )
        down_steps.insert(0, compiler.compile_drop_index(index.name, table_sql))


def _constraint_steps(state: _Generation) -> None:
    """Adds, changes, and drops constraints in place."""
    compiler = state.compiler
    diff = state.diff
    actual = state.actual
    models_by_table = state.models_by_table
    allow_drops = state.allow_drops
    up_steps = state.up_steps
    down_steps = state.down_steps
    rebuild_tables = state.rebuild_tables
    # Constraint changes on dialects that alter in place. A table headed
    # for a rebuild gets its constraints from the rebuilt CREATE TABLE.
    for model, check in diff.new_checks:
        if (model.tableName or "").lower() in rebuild_tables:
            continue
        table_sql = model._qualified_table_sql(compiler)
        up_steps.append(
            with_intent(
                compiler.compile_add_check(table_sql, check.name, check.expression),
                "add_check",
                _intent_table(model),
                name=check.name,
            )
        )
        down_steps.insert(0, compiler.compile_drop_constraint(table_sql, check.name))
    for model, fk in diff.new_foreign_keys:
        if (model.tableName or "").lower() in rebuild_tables:
            continue
        table_sql = model._qualified_table_sql(compiler)
        up_steps.append(
            _declared_fk_intent(
                _declared_fk_sql(compiler, table_sql, fk), _intent_table(model), fk
            )
        )
        down_steps.insert(0, compiler.compile_drop_foreign_key(table_sql, fk.name))
    if allow_drops:
        for model, fk, actual_fk in diff.changed_foreign_keys:
            if (model.tableName or "").lower() in rebuild_tables:
                continue
            table_sql = model._qualified_table_sql(compiler)
            intent_table = _intent_table(model)
            up_steps.append(
                with_intent(
                    compiler.compile_drop_foreign_key(table_sql, fk.name),
                    "drop_foreign_key",
                    intent_table,
                    name=fk.name,
                )
            )
            up_steps.append(
                _declared_fk_intent(
                    _declared_fk_sql(compiler, table_sql, fk), intent_table, fk
                )
            )
            restore = _introspected_fk_sql(
                compiler, table_sql, fk.name, actual_fk, actual, model.tableName or ""
            )
            if restore is None:
                state.reversible = False
            else:
                down_steps.insert(0, restore)
                down_steps.insert(
                    0, compiler.compile_drop_foreign_key(table_sql, fk.name)
                )
        for table, name, actual_fk in diff.extra_foreign_keys:
            if table.lower() in rebuild_tables:
                continue
            table_sql = _declared_table_sql(compiler, models_by_table, actual, table)
            up_steps.append(
                with_intent(
                    compiler.compile_drop_foreign_key(table_sql, name),
                    "drop_foreign_key",
                    _reported_intent_table(models_by_table, actual, table),
                    name=name,
                )
            )
            restore = _introspected_fk_sql(
                compiler, table_sql, name, actual_fk, actual, table
            )
            if restore is None:
                state.reversible = False
            else:
                down_steps.insert(0, restore)
        for table, name, expression in diff.extra_checks:
            if table.lower() in rebuild_tables:
                continue
            table_sql = _declared_table_sql(compiler, models_by_table, actual, table)
            up_steps.append(
                with_intent(
                    compiler.compile_drop_constraint(table_sql, name),
                    "drop_constraint",
                    _reported_intent_table(models_by_table, actual, table),
                    name=name,
                )
            )
            down_steps.insert(
                0, compiler.compile_add_check(table_sql, name, expression)
            )


def _drop_steps(state: _Generation) -> None:
    """Drops the extra indexes, columns, tables, and enum types."""
    compiler = state.compiler
    diff = state.diff
    actual = state.actual
    models_by_table = state.models_by_table
    allow_drops = state.allow_drops
    up_steps = state.up_steps
    rebuild_tables = state.rebuild_tables
    down_steps = state.down_steps
    if allow_drops:
        for table, name, actual_index in diff.extra_indexes:
            if table.lower() in rebuild_tables:
                continue
            table_sql = _declared_table_sql(compiler, models_by_table, actual, table)
            intent_table = _reported_intent_table(models_by_table, actual, table)
            actual_table = actual[table.lower()]
            if actual_index.constraint:
                # The index belongs to a UNIQUE constraint, and the engine
                # refuses DROP INDEX on it.
                up_steps.append(
                    with_intent(
                        compiler.compile_drop_constraint(table_sql, name),
                        "drop_constraint",
                        intent_table,
                        name=name,
                    )
                )
                down_steps.insert(
                    0,
                    compiler.compile_add_unique(
                        table_sql, name, _spelled_columns(actual_table, actual_index)
                    ),
                )
                continue
            up_steps.append(
                with_intent(
                    compiler.compile_drop_index(name, table_sql),
                    "drop_index",
                    intent_table,
                    name=name,
                )
            )
            down_steps.insert(
                0,
                compiler.compile_create_index(
                    name,
                    table_sql,
                    _spelled_columns(actual_table, actual_index),
                    actual_index.unique,
                ),
            )
        for table, name in diff.extra_columns:
            if table.lower() in rebuild_tables:
                continue
            table_sql = _declared_table_sql(compiler, models_by_table, actual, table)
            up_steps.append(
                with_intent(
                    compiler.compile_drop_column(table_sql, name),
                    "drop_column",
                    _reported_intent_table(models_by_table, actual, table),
                    name,
                )
            )
            state.reversible = False
        if diff.extra_tables:
            drops, bare = _extra_table_drops(compiler, actual, diff.extra_tables)
            up_steps.extend(drops)
            if bare:
                state.transactional = False
            state.reversible = False
        # A type drops after every table and column that used it.
        for type_name in diff.extra_enum_types:
            up_steps.append(
                with_intent(
                    compiler.compile_drop_enum_type(type_name),
                    "drop_enum_type",
                    None,
                    name=type_name,
                )
            )


def _created_enum_type_downs(state: _Generation) -> None:
    """Drops the enum types this migration created."""
    compiler = state.compiler
    down_steps = state.down_steps
    # Types created in this migration drop last on the way down, after
    # every table that referenced them is gone.
    for type_name in state.created_enum_types:
        down_steps.append(compiler.compile_drop_enum_type(type_name))
