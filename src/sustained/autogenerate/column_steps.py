"""
The phases of autogenerate() that work on columns: enum checks, index
lifts around ALTER COLUMN, changed, added, and commented columns.
"""

from __future__ import annotations

from sustained.analysis import MigrationStatement, with_intent
from sustained.autogenerate.diff import _column_type_changed
from sustained.autogenerate.statements import (
    _add_enum_check,
    _add_foreign_key,
    _intent_table,
    _introspected_state,
    _lift_statements,
    _lifted_indexes,
    _preserving_state,
    _rebuild_needed,
    _refuse_enum_value_removal,
    _relaxed_copy,
    _table_has_rows,
    _tagged,
)
from sustained.autogenerate.steps import _Generation
from sustained.exceptions import DialectError
from sustained.rebuild import add_column_needs_rebuild
from sustained.schema import ColumnState, bare_table_name, render_column_sql
from sustained.type_changes import type_change_loses_data


def _enum_checks_off(state: _Generation) -> None:
    """Takes off the enum checks whose values changed."""
    compiler = state.compiler
    diff = state.diff
    up_steps = state.up_steps
    down_steps = state.down_steps
    rebuild_tables = state.rebuild_tables
    # An enum check whose values changed comes off before the column
    # changes and goes back on after them: SQL Server refuses to alter a
    # column that a CHECK constraint names, and a longer value widens the
    # VARCHAR in the same migration. A dialect that cannot alter in place
    # rebuilds the table, and the rebuilt CREATE TABLE writes the check.
    for model, name, _, expression in diff.changed_enum_checks:
        if _rebuild_needed(compiler, "change a constraint"):
            rebuild_tables[(model.tableName or "").lower()] = model
            continue
        if expression is not None:
            table_sql = model._qualified_table_sql(compiler)
            constraint = f"ck_{bare_table_name(model.tableName or '')}_{name}_enum"
            up_steps.append(
                with_intent(
                    compiler.compile_drop_constraint(table_sql, constraint),
                    "drop_constraint",
                    _intent_table(model),
                    name,
                    name=constraint,
                )
            )
            down_steps.insert(
                0, compiler.compile_add_check(table_sql, constraint, expression)
            )
        state.enum_check_adds.append((model, name))


def _lift_index_steps(state: _Generation) -> None:
    """Drops the indexes an ALTER COLUMN cannot run under."""
    compiler = state.compiler
    diff = state.diff
    actual = state.actual
    models_by_table = state.models_by_table
    up_steps = state.up_steps
    down_steps = state.down_steps
    ignore_changed_columns = state.ignore_changed_columns
    lift_drops = state.lift_drops
    lift_creates = state.lift_creates
    # An engine that refuses ALTER COLUMN while an index depends on the
    # column, or on the table, gets those indexes dropped before the
    # column changes and created again after the new columns are in.
    # The down steps wrap the reversing statements the same way.
    for table_key, index_name, lifted in _lifted_indexes(
        compiler, diff, actual, ignore_changed_columns
    ):
        drop_sql, create_sql = _lift_statements(
            compiler,
            models_by_table[table_key]._qualified_table_sql(compiler),
            actual[table_key],
            index_name,
            lifted,
        )
        lift_drops.append(drop_sql)
        lift_creates.append(create_sql)
    up_steps.extend(lift_drops)
    down_steps[0:0] = lift_creates
    if lift_drops and compiler.index_drop_waits_for_commit():
        state.transactional = False


def _changed_column_steps(state: _Generation) -> None:
    """ALTER COLUMN for type and nullability changes."""
    compiler = state.compiler
    diff = state.diff
    actual = state.actual
    models_by_table = state.models_by_table
    up_steps = state.up_steps
    down_steps = state.down_steps
    rebuild_tables = state.rebuild_tables
    type_casts = state.type_casts
    ignore_changed_columns = state.ignore_changed_columns
    restated_states = state.restated_states
    # Changed columns: ALTER in place where the dialect can, otherwise
    # mark the table for a rebuild.
    # The state each changed column is left in by its type and
    # nullability statements, for a comment statement that restates the
    # whole column after them.
    if not ignore_changed_columns:
        for table, name, actual_desc, expected_desc in diff.changed_columns:
            model = models_by_table[table.lower()]
            assert model.tableColumns is not None
            coldef = model.tableColumns[name]
            actual_col = actual[table.lower()].columns[name.lower()]
            if _rebuild_needed(compiler, "change a column"):
                rebuild_tables[table.lower()] = model
                continue
            table_sql = model._qualified_table_sql(compiler)
            intent_table = _intent_table(model)
            expected_type = compiler.compile_column_type(coldef)
            if _column_type_changed(compiler, coldef, expected_type, actual_col):
                using = type_casts.get(f"{table}.{name}")
                _refuse_enum_value_removal(compiler, table, name, coldef, actual_col)
                # A narrowing change converts every value, and the down
                # step gives back the type but not what the conversion
                # cut, so the statements carry the destructive mark.
                lossy = type_change_loses_data(
                    compiler, coldef, expected_type, actual_col.raw_type
                )
                # A type change keeps the nullability the table has now.
                # Tightening to NOT NULL is a separate step that runs
                # after the backfill, and on MySQL and SQL Server the
                # restated definition would otherwise apply it early.
                changed_state = _preserving_state(
                    compiler, coldef, actual_col, expected_type, actual_col.nullable
                )
                restated_states[(table.lower(), name.lower())] = changed_state
                # SQL Server refuses a type change on a column that has a
                # default, so the default comes off around it both ways.
                default_sql = actual_col.restated_default()
                lift_default = (
                    default_sql is not None and not compiler.alter_type_keeps_default()
                )
                if lift_default:
                    up_steps.append(
                        with_intent(
                            compiler.compile_drop_column_default(table_sql, name),
                            "drop_column_default",
                            intent_table,
                            name,
                        )
                    )
                up_steps.extend(
                    with_intent(
                        MigrationStatement(statement, destructive=lossy),
                        "alter_column_type",
                        intent_table,
                        name,
                        from_type=actual_col.raw_type,
                        to_type=expected_type,
                        using=using,
                        lossy=lossy,
                    )
                    for statement in compiler.compile_alter_column_type(
                        table_sql, name, changed_state, using
                    )
                )
                if lift_default:
                    assert default_sql is not None
                    add_default = compiler.compile_add_column_default(
                        table_sql, name, default_sql
                    )
                    up_steps.append(
                        with_intent(
                            add_default, "set_column_default", intent_table, name
                        )
                    )
                    down_steps.insert(0, add_default)
                for statement in reversed(
                    compiler.compile_alter_column_type(
                        table_sql,
                        name,
                        _preserving_state(
                            compiler,
                            coldef,
                            actual_col,
                            actual_col.raw_type,
                            actual_col.nullable,
                        ),
                    )
                ):
                    down_steps.insert(0, statement)
                if lift_default:
                    down_steps.insert(
                        0, compiler.compile_drop_column_default(table_sql, name)
                    )
            if actual_col.nullable != coldef.nullable and not coldef.primary_key:
                if not coldef.nullable:
                    filler = (
                        coldef.backfill
                        if coldef.backfill is not None
                        else coldef.default
                    )
                    if filler is None:
                        raise ValueError(
                            f"Tightening '{table}.{name}' to NOT NULL needs "
                            "a backfill or default value for existing NULLs."
                        )
                    up_steps.extend(
                        _tagged(
                            compiler.compile_backfill(
                                table_sql,
                                name,
                                expected_type,
                                compiler.format_value(filler),
                            ),
                            "backfill",
                            intent_table,
                            name,
                        )
                    )
                changed_state = _preserving_state(
                    compiler, coldef, actual_col, expected_type, coldef.nullable
                )
                restated_states[(table.lower(), name.lower())] = changed_state
                up_steps.extend(
                    _tagged(
                        compiler.compile_alter_column_nullability(
                            table_sql, name, changed_state
                        ),
                        "drop_not_null" if coldef.nullable else "set_not_null",
                        intent_table,
                        name,
                    )
                )
                for statement in reversed(
                    compiler.compile_alter_column_nullability(
                        table_sql,
                        name,
                        _preserving_state(
                            compiler,
                            coldef,
                            actual_col,
                            expected_type,
                            actual_col.nullable,
                        ),
                    )
                ):
                    down_steps.insert(0, statement)


def _enum_checks_on(state: _Generation) -> None:
    """Puts back the enum checks _enum_checks_off() took off."""
    compiler = state.compiler
    up_steps = state.up_steps
    down_steps = state.down_steps
    for model, name in state.enum_check_adds:
        assert model.tableColumns is not None
        _add_enum_check(
            compiler,
            up_steps,
            down_steps,
            model._qualified_table_sql(compiler),
            model,
            name,
            model.tableColumns[name],
        )


def _add_column_rebuild_scan(state: _Generation) -> None:
    """Marks the tables SQLite cannot ADD COLUMN to for a rebuild."""
    compiler = state.compiler
    diff = state.diff
    rebuild_tables = state.rebuild_tables
    # SQLite refuses some columns in ADD COLUMN, and the rebuilt CREATE
    # TABLE takes them. The scan runs before any column is added, so a
    # table headed for a rebuild gets no ADD COLUMN for its other new
    # columns either.
    if compiler.rebuild_strategy() == "rebuild":
        for model, _, coldef in diff.new_columns:
            if add_column_needs_rebuild(compiler, coldef):
                rebuild_tables[(model.tableName or "").lower()] = model


def _new_column_steps(state: _Generation) -> None:
    """ADD COLUMN for each new column."""
    connection = state.connection
    compiler = state.compiler
    diff = state.diff
    up_steps = state.up_steps
    down_steps = state.down_steps
    rebuild_tables = state.rebuild_tables
    # New columns. A NOT NULL column with no value for the rows already
    # there fails the same way on both paths, so the check runs before a
    # table headed for a rebuild is skipped. The rebuild would otherwise
    # copy NULL into the column and fail with a bare constraint error. An
    # empty table has no such rows and takes the column.
    for model, name, coldef in diff.new_columns:
        table_key = (model.tableName or "").lower()
        rebuilding = table_key in rebuild_tables
        if (
            not coldef.nullable
            and coldef.default is None
            and coldef.backfill is None
            and not coldef.primary_key
            and _table_has_rows(
                connection, compiler, model._qualified_table_sql(compiler)
            )
        ):
            raise ValueError(
                f"Cannot add NOT NULL column '{model.tableName}.{name}' "
                "without a default or backfill; existing rows would "
                "have no value."
            )
        if rebuilding:
            continue
        if coldef.primary_key or coldef.autoincrement:
            raise ValueError(
                f"Cannot add '{model.tableName}.{name}' with ALTER TABLE: "
                "primary key and autoincrement columns need a hand-written "
                "migration."
            )
        table_sql = model._qualified_table_sql(compiler)
        intent_table = _intent_table(model)
        if not coldef.nullable and coldef.default is None:
            # A dialect that rebuilds took this table in the scan above.
            # One that can neither alter nor rebuild refuses here.
            _rebuild_needed(compiler, "add a NOT NULL column")
            # Add nullable, backfill, then tighten.
            relaxed = render_column_sql(
                compiler,
                name,
                _relaxed_copy(coldef),
                inline_pk=False,
            )
            up_steps.append(
                with_intent(
                    compiler.compile_add_column(table_sql, relaxed),
                    "add_column",
                    intent_table,
                    name,
                    nullable=True,
                    has_default=False,
                )
            )
            up_steps.extend(
                _tagged(
                    compiler.compile_backfill(
                        table_sql,
                        name,
                        compiler.compile_column_type(coldef),
                        compiler.format_value(coldef.backfill),
                    ),
                    "backfill",
                    intent_table,
                    name,
                )
            )
            up_steps.extend(
                _tagged(
                    compiler.compile_alter_column_nullability(
                        table_sql,
                        name,
                        ColumnState.from_column(compiler, coldef, nullable=False),
                    ),
                    "set_not_null",
                    intent_table,
                    name,
                )
            )
            down_steps.insert(0, compiler.compile_drop_column(table_sql, name))
            _add_enum_check(
                compiler, up_steps, down_steps, table_sql, model, name, coldef
            )
            _add_foreign_key(
                compiler, up_steps, down_steps, table_sql, model, name, coldef
            )
            continue
        column_sql = render_column_sql(compiler, name, coldef, inline_pk=False)
        up_steps.append(
            with_intent(
                compiler.compile_add_column(table_sql, column_sql),
                "add_column",
                intent_table,
                name,
                nullable=coldef.nullable,
                has_default=coldef.default is not None,
            )
        )
        down_steps.insert(0, compiler.compile_drop_column(table_sql, name))
        _add_enum_check(compiler, up_steps, down_steps, table_sql, model, name, coldef)
        _add_foreign_key(compiler, up_steps, down_steps, table_sql, model, name, coldef)


def _restore_lifted_index_steps(state: _Generation) -> None:
    """Creates again the indexes _lift_index_steps() dropped."""
    up_steps = state.up_steps
    down_steps = state.down_steps
    lift_drops = state.lift_drops
    lift_creates = state.lift_creates
    up_steps.extend(lift_creates)
    down_steps[0:0] = lift_drops


def _comment_steps(state: _Generation) -> None:
    """The comment statements for each drifted column comment."""
    compiler = state.compiler
    diff = state.diff
    actual = state.actual
    models_by_table = state.models_by_table
    up_steps = state.up_steps
    down_steps = state.down_steps
    restated_states = state.restated_states
    # Comment changes. The down step writes the database's old comment
    # back. MySQL restates the whole column, so both directions restate
    # the column as the table has it: the state the type and nullability
    # statements above leave, or else the catalog's own report. The
    # model's declaration would change the type or the default in a
    # statement meant for the comment, and down would not change it back.
    for table, name, actual_comment, expected_comment in diff.changed_comments:
        model = models_by_table[table.lower()]
        assert model.tableColumns is not None
        coldef = model.tableColumns[name]
        table_sql = model._qualified_table_sql(compiler)
        column_state = restated_states.get((table.lower(), name.lower()))
        if column_state is None:
            column_state = _introspected_state(
                actual[table.lower()].columns[name.lower()]
            )
        try:
            set_new = compiler.compile_set_column_comment(
                table_sql, name, expected_comment, coldef, column_state
            )
            set_old = compiler.compile_set_column_comment(
                table_sql, name, actual_comment, coldef, column_state
            )
        except DialectError as error:
            # Athena reports comments but cannot change one in place.
            # The drift is real and worth saying, but it must not stop
            # the rest of the migration from being generated.
            diff.constraint_notes.append(
                f"{table}.{name} comment is {actual_comment or 'none'}, "
                f"model declares {expected_comment or 'none'}: {error}"
            )
            continue
        up_steps.extend(
            _tagged(set_new, "set_column_comment", _intent_table(model), name)
        )
        for statement in reversed(set_old):
            down_steps.insert(0, statement)
