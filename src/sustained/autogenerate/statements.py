"""
The small builders autogenerate()'s phases share: table names for DDL
and Intents, CREATE TABLE and foreign key statements, index lifts, the
row probe, and the column states a restating statement carries.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
)

from sustained.analysis import with_intent
from sustained.autogenerate.diff import SchemaDiff, _dependency_order
from sustained.exceptions import DialectError
from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
)
from sustained.migrations import _ReplayConnection
from sustained.rebuild import create_indexes_sql
from sustained.schema import (
    ColumnState,
    ForeignKey,
    bare_table_name,
    build_create_table_sql,
    enum_check_constraint_sql,
)
from sustained.type_changes import removed_enum_values
from sustained.types import Connection

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.model import Model
    from sustained.schema import ColumnDef, Index


def _snapshot_table_sql(
    compiler: "Compiler", table: IntrospectedTable, key: str
) -> str:
    """
    The DDL name of a table the snapshot read and no model declares: its
    schema in front when the read kept one, and the catalog's spelling.
    """
    parts = [table.name or key]
    if table.schema is not None:
        parts.insert(0, table.schema)
    return ".".join(compiler.quote_ddl_identifier(part) for part in parts)


def _declared_table_sql(
    compiler: "Compiler",
    models_by_table: Mapping[str, Type["Model"]],
    actual: Snapshot,
    table: str,
) -> str:
    """
    The DDL name of a table the diff reports by name. A table a model
    declares takes the model's schema and database, and any other table
    takes what the snapshot read.
    """
    model = models_by_table.get(table.lower())
    if model is not None:
        return model._qualified_table_sql(compiler)
    return _snapshot_table_sql(compiler, actual[table.lower()], table)


def _intent_table(model: Type["Model"]) -> str:
    """The dotted, unquoted table name an Intent gives a model's table."""
    parts = [model.database, model.tableSchema, model.tableName]
    return ".".join(p for p in parts if p)


def _reported_intent_table(
    models_by_table: Mapping[str, Type["Model"]], actual: Snapshot, table: str
) -> str:
    """
    The Intent table name of a table the diff reports by name: the
    model's name for a declared table, else the snapshot's.
    """
    model = models_by_table.get(table.lower())
    if model is not None:
        return _intent_table(model)
    read = actual.get(table.lower())
    if read is None:
        return table
    parts = [read.name or table]
    if read.schema is not None:
        parts.insert(0, read.schema)
    return ".".join(parts)


def _tagged(
    statements: Sequence[str],
    kind: str,
    table: Optional[str],
    column: Optional[str] = None,
    **details: object,
) -> List[str]:
    """Every statement tagged with the same Intent."""
    return [with_intent(s, kind, table, column, **details) for s in statements]


def _extra_table_drops(
    compiler: "Compiler", actual: Snapshot, extra_tables: List[str]
) -> Tuple[List[str], bool]:
    """
    The statements that drop the tables no model declares, a table
    before the tables it points at. The engine refuses to drop a table
    that another table's foreign key still names. Tables that point at
    each other in a cycle have no such order. Where the engine can drop
    a constraint, their keys go first. SQLite cannot, so there the drops
    run with foreign key enforcement off. The second value says whether
    they need that, which holds only outside a transaction.
    """
    keys = [table.lower() for table in extra_tables]
    spelled = {key: _snapshot_table_sql(compiler, actual[key], key) for key in keys}
    # The order puts a table after the tables that point at it, so a
    # table no key names keeps its place in the catalog's order.
    children: Dict[str, List[str]] = {key: [] for key in keys}
    for key in keys:
        for fk in actual[key].foreign_keys.values():
            target = fk.target_table
            if target in children and target != key and key not in children[target]:
                children[target].append(key)
    ordered, cycles = _dependency_order(keys, lambda key: children[key])
    named = {key: _reported_intent_table({}, actual, key) for key in keys}
    drops: List[str] = [
        with_intent(f"DROP TABLE {spelled[key]}", "drop_table", named[key])
        for key in ordered
    ]
    in_cycle = {key for cycle in cycles for key in cycle}
    if not in_cycle:
        return drops, False
    if not compiler.supports_add_constraint():
        return (
            _foreign_keys_setting(compiler.rebuild_setup_sql(), "OFF")
            + drops
            + _foreign_keys_setting(compiler.rebuild_finish_sql(), "ON"),
            True,
        )
    key_drops: List[str] = [
        with_intent(
            compiler.compile_drop_foreign_key(spelled[key], fk.name or name),
            "drop_foreign_key",
            named[key],
            name=fk.name or name,
        )
        for key in keys
        if key in in_cycle
        for name, fk in actual[key].foreign_keys.items()
        if fk.target_table in in_cycle and fk.target_table != key
    ]
    return key_drops + drops, False


def _foreign_keys_setting(statements: Sequence[str], value: str) -> List[str]:
    """The pragmas that turn SQLite's foreign key enforcement off or on."""
    return _tagged(
        statements, "session_setting", None, setting="foreign_keys", value=value
    )


def _create_table_steps(
    compiler: "Compiler", model: Type["Model"], defer_foreign_keys: bool
) -> List[str]:
    """
    The statements that build one missing table: CREATE TABLE, the
    column comments a dialect keeps as separate statements, and the
    model's indexes. Enum types are created once for the whole
    migration, so they are not repeated here.
    """
    from sustained.schema import column_comment_statements

    assert model.tableColumns is not None
    table_sql = model._qualified_table_sql(compiler)
    table = _intent_table(model)
    statements: List[str] = [
        with_intent(
            build_create_table_sql(
                compiler,
                table_sql,
                model.tableColumns,
                options=model.tableOptions,
                constraints=model.tableConstraints,
                defer_foreign_keys=defer_foreign_keys,
            ),
            "create_table",
            table,
        )
    ]
    statements.extend(
        _tagged(
            column_comment_statements(compiler, table_sql, model.tableColumns),
            "set_column_comment",
            table,
        )
    )
    for index, statement in zip(
        model.indexes or [], create_indexes_sql(compiler, model)
    ):
        statements.append(_index_intent(statement, table, index))
    return statements


def _index_intent(statement: str, table: str, index: "Index") -> str:
    """A CREATE INDEX statement tagged with the index it builds."""
    return with_intent(
        statement,
        "create_index",
        table,
        name=index.name,
        columns=tuple(index.columns),
        unique=index.unique,
    )


def _deferred_foreign_key_steps(
    compiler: "Compiler", model: Type["Model"]
) -> List[Tuple[str, str]]:
    """
    The (add, drop) statement pairs for every foreign key a new table
    needs, once CREATE TABLE has left them out.
    """
    table_sql = model._qualified_table_sql(compiler)
    table = _intent_table(model)
    pairs: List[Tuple[str, str]] = []
    for name, coldef in (model.tableColumns or {}).items():
        if coldef.references is None:
            continue
        ref_table, ref_column = coldef.references.rsplit(".", 1)
        constraint = f"fk_{model.tableName}_{name}"
        pairs.append(
            (
                with_intent(
                    compiler.compile_add_foreign_key(
                        table_sql,
                        constraint,
                        name,
                        compiler.quote_fully_qualified_ddl_identifier(ref_table),
                        ref_column,
                    ),
                    "add_foreign_key",
                    table,
                    name=constraint,
                    references=ref_table,
                ),
                compiler.compile_drop_foreign_key(table_sql, constraint),
            )
        )
    for constraint_def in model.tableConstraints or []:
        if not isinstance(constraint_def, ForeignKey):
            continue
        pairs.append(
            (
                _declared_fk_intent(
                    _declared_fk_sql(compiler, table_sql, constraint_def),
                    table,
                    constraint_def,
                ),
                compiler.compile_drop_foreign_key(table_sql, constraint_def.name),
            )
        )
    return pairs


def _declared_fk_intent(statement: str, table: str, fk: ForeignKey) -> str:
    """An ADD CONSTRAINT statement tagged with the foreign key it adds."""
    return with_intent(
        statement,
        "add_foreign_key",
        table,
        name=fk.name,
        references=fk.target_table,
    )


def _refuse_enum_value_removal(
    compiler: "Compiler",
    table: str,
    name: str,
    coldef: "ColumnDef",
    actual_col: IntrospectedColumn,
) -> None:
    """
    Raises ValueError when a MySQL enum column would lose values. MySQL
    rewrites a row that holds a removed value to '' outside strict mode,
    and refuses the MODIFY in strict mode, so no generated statement
    removes a value safely.
    """
    if compiler.enum_strategy() != "inline":
        return
    removed = removed_enum_values(coldef, actual_col.raw_type)
    if removed:
        values = ", ".join(f"'{value}'" for value in removed)
        raise ValueError(
            f"The model removes {values} from the enum column "
            f"'{table}.{name}'. MySQL rewrites rows holding a removed value "
            "to '' or refuses the change, so it is not generated. Move those "
            "rows to a kept value and change the column in a migration "
            "you write, or keep the values in the model."
        )


def _lifted_indexes(
    compiler: "Compiler",
    diff: SchemaDiff,
    actual: Snapshot,
    ignore_changed_columns: bool,
) -> List[Tuple[str, str, IntrospectedIndex]]:
    """
    The indexes that come off a table while an ALTER COLUMN statement
    changes it, as (table key, index name, index). Those are the ones
    the compiler's alter_column_index_scope() names, on the tables whose
    columns change type or nullability, and on the tables that gain a
    NOT NULL column through add, backfill, and tighten. A UNIQUE
    constraint comes off only where the engine can add it back.
    """
    scope = compiler.alter_column_index_scope()
    if scope == "none":
        return []
    altered: Dict[str, Set[str]] = {}
    if not ignore_changed_columns:
        for table, name, _, _ in diff.changed_columns:
            altered.setdefault(table.lower(), set()).add(name.lower())
    for model, name, coldef in diff.new_columns:
        if not coldef.nullable and coldef.default is None:
            key = (model.tableName or "").lower()
            altered.setdefault(key, set()).add(name.lower())
    lifted: List[Tuple[str, str, IntrospectedIndex]] = []
    for table_key, columns in altered.items():
        for name, index in actual[table_key].indexes.items():
            if index.constraint and not compiler.supports_add_constraint():
                continue
            if scope == "table" or columns & set(index.columns):
                lifted.append((table_key, index.name or name, index))
    return lifted


def _lift_statements(
    compiler: "Compiler",
    table_sql: str,
    table: IntrospectedTable,
    name: str,
    index: IntrospectedIndex,
) -> Tuple[str, str]:
    """The statements that drop one lifted index and create it again."""
    columns = _spelled_columns(table, index)
    intent_table = ".".join(p for p in (table.schema, table.name) if p)
    if index.constraint:
        return (
            with_intent(
                compiler.compile_drop_constraint(table_sql, name),
                "drop_constraint",
                intent_table,
                name=name,
            ),
            with_intent(
                compiler.compile_add_unique(table_sql, name, columns),
                "add_unique",
                intent_table,
                name=name,
                columns=tuple(columns),
            ),
        )
    return (
        with_intent(
            compiler.compile_drop_index(name, table_sql),
            "drop_index",
            intent_table,
            name=name,
        ),
        with_intent(
            compiler.compile_create_index(name, table_sql, columns, index.unique),
            "create_index",
            intent_table,
            name=name,
            columns=tuple(columns),
            unique=index.unique,
        ),
    )


def _rebuild_needed(compiler: "Compiler", change: str) -> bool:
    """
    Whether a change the dialect cannot make with ALTER TABLE has to go
    through a table rebuild. A dialect that can neither alter nor
    rebuild refuses here, rather than emitting a plan of statements it
    does not have.
    """
    strategy = compiler.rebuild_strategy()
    if strategy == "alter":
        return False
    if strategy == "unsupported":
        raise DialectError(
            f"{compiler.dialect_name().title()} cannot {change} in place, "
            "and it cannot rebuild a table either. Write the migration by "
            "hand: create the new table, copy the rows across with "
            "INSERT INTO ... SELECT, and swap the names."
        )
    return True


def _can_probe_rows(connection: Connection) -> bool:
    """
    Whether a connection can run the row probe.

    A recorded schema read answers the statements of the read and
    nothing else. The async path hands one of those in place of a
    connection, so the probe has no way to run there.
    """
    return not isinstance(connection, _ReplayConnection)


def _table_has_rows(
    connection: Connection, compiler: "Compiler", table_sql: str
) -> bool:
    """
    Whether the table holds a row. A NOT NULL column with no default and
    no backfill has no value for the rows already there, but an empty
    table has no such rows and takes the column.

    A read that fails answers True, so the refusal stands whenever the
    table cannot be read.

    A connection that replays a recorded schema read runs no statement of
    its own, so the probe cannot run on it and the answer is True. The
    async migrator reads the schema through its adapter and replays the
    recording here, so it always refuses such a column, where the
    blocking path takes it on an empty table.
    """
    from sustained.execution import cursor_scope

    if not _can_probe_rows(connection):
        return True
    try:
        top = f"{compiler.compile_top(1)} "
        limit = ""
    except DialectError:
        top = ""
        limit = f" {compiler.compile_limit_offset(1, None)}"
    try:
        with cursor_scope(connection) as cursor:
            cursor.execute(f"SELECT {top}1 FROM {table_sql}{limit}")
            return bool(cursor.fetchall())
    except Exception:
        return True


def _declared_fk_sql(compiler: "Compiler", table_sql: str, fk: ForeignKey) -> str:
    """Renders a declared ForeignKey as an ADD CONSTRAINT statement."""
    return compiler.compile_add_foreign_key(
        table_sql,
        fk.name,
        fk.columns,
        compiler.quote_fully_qualified_ddl_identifier(fk.target_table),
        fk.target_columns,
        fk.on_delete,
        fk.on_update,
    )


def _spelled_columns(table: IntrospectedTable, index: IntrospectedIndex) -> List[str]:
    """An introspected index's columns as the catalog spells them."""
    return [table.spelled_column(column) for column in index.columns]


def _introspected_fk_sql(
    compiler: "Compiler",
    table_sql: str,
    name: str,
    fk: IntrospectedForeignKey,
    snapshot: Snapshot,
    table: str,
) -> Optional[str]:
    """
    Renders an introspected foreign key on `table` back into an ADD
    CONSTRAINT statement, for the down step of a drop. None when the
    catalog did not say where the key points, which makes the drop
    irreversible. An empty target column list renders without one: the
    key references the target table's primary key. Names are spelled as
    the snapshot spells them, where it has the table. A target outside
    the connection's schema takes its schema in front.
    """
    if fk.target_table == "?":
        return None
    on_delete = None if fk.on_delete is None else fk.on_delete.upper()
    on_update = None if fk.on_update is None else fk.on_update.upper()
    source = snapshot.get(table.lower())
    target = snapshot.get(fk.target_table)
    columns = list(fk.columns)
    target_columns = list(fk.target_columns)
    target_parts = [fk.target_table]
    if source is not None:
        columns = [source.spelled_column(column) for column in columns]
    if target is not None:
        target_columns = [target.spelled_column(column) for column in target_columns]
        target_parts = [target.name or fk.target_table]
    target_schema = fk.target_schema or (None if target is None else target.schema)
    if target_schema is not None:
        target_parts.insert(0, target_schema)
    return compiler.compile_add_foreign_key(
        table_sql,
        name,
        columns,
        ".".join(compiler.quote_ddl_identifier(part) for part in target_parts),
        target_columns,
        None if on_delete == "NO ACTION" else on_delete,
        None if on_update == "NO ACTION" else on_update,
    )


def _add_foreign_key(
    compiler: "Compiler",
    up_steps: List[str],
    down_steps: List[str],
    table_sql: str,
    model: Type["Model"],
    name: str,
    coldef: "ColumnDef",
) -> None:
    """
    Adds the foreign key of a newly added column as its own statement, on
    a dialect where a REFERENCES clause beside the column creates nothing.
    The constraint takes a name, so the down step can name it back.
    """
    if coldef.references is None or compiler.inline_references():
        return
    ref_table, ref_column = coldef.references.rsplit(".", 1)
    constraint = f"fk_{model.tableName}_{name}"
    up_steps.append(
        with_intent(
            compiler.compile_add_foreign_key(
                table_sql,
                constraint,
                name,
                compiler.quote_fully_qualified_ddl_identifier(ref_table),
                ref_column,
            ),
            "add_foreign_key",
            _intent_table(model),
            name=constraint,
            references=ref_table,
        )
    )
    down_steps.insert(0, compiler.compile_drop_foreign_key(table_sql, constraint))


def _add_enum_check(
    compiler: "Compiler",
    up_steps: List[str],
    down_steps: List[str],
    table_sql: str,
    model: Type["Model"],
    name: str,
    coldef: "ColumnDef",
) -> None:
    """
    Adds the named CHECK constraint that holds a newly added enum column
    to its values, on dialects where an enum is a checked VARCHAR. The
    down drops the constraint before the column it checks.
    """
    if coldef.type_name != "ENUM" or compiler.enum_strategy() != "check":
        return
    from sustained.schema import bare_table_name

    table_name = bare_table_name(table_sql)
    constraint_sql = enum_check_constraint_sql(compiler, table_name, name, coldef)
    up_steps.append(
        with_intent(
            f"ALTER TABLE {table_sql} ADD {constraint_sql}",
            "add_check",
            _intent_table(model),
            name,
            name=f"ck_{table_name}_{name}_enum",
        )
    )
    down_steps.insert(
        0,
        compiler.compile_drop_constraint(table_sql, f"ck_{table_name}_{name}_enum"),
    )


def _relaxed_copy(coldef: "ColumnDef") -> "ColumnDef":
    """A nullable copy of a ColumnDef, used for add-then-tighten steps."""
    from sustained.schema import ColumnDef

    return ColumnDef(
        coldef.type_name,
        length=coldef.length,
        precision=coldef.precision,
        scale=coldef.scale,
        nullable=True,
        unique=coldef.unique,
        default=coldef.default,
        references=coldef.references,
        enum_name=coldef.enum_name,
        enum_values=coldef.enum_values,
    )


def _introspected_state(column: IntrospectedColumn) -> ColumnState:
    """The state a column is in today, as the catalog reports it."""
    return ColumnState(
        type_sql=column.raw_type,
        nullable=column.nullable,
        default_sql=column.restated_default(),
        comment=column.comment,
        autoincrement=column.autoincrement,
        collation=column.collation,
        on_update=column.on_update,
    )


def _preserving_state(
    compiler: "Compiler",
    coldef: "ColumnDef",
    actual_col: IntrospectedColumn,
    type_sql: str,
    nullable: bool,
) -> ColumnState:
    """The state a type or nullability statement restates a column in."""
    # MySQL and SQL Server restate the whole column definition, so a
    # statement aimed at the type or the nullability must carry the
    # default and the comment the table has today, not the model's.
    # A default or comment drift stays a note on the diff; folding it
    # into this statement would change it silently, and the down step
    # would write the model's value over the one the column held.
    state = ColumnState.from_column(
        compiler, coldef, type_sql=type_sql, nullable=nullable
    )
    return state._replace(
        default_sql=actual_col.restated_default(),
        comment=(actual_col.comment if compiler.stores_column_comments() else None),
        collation=actual_col.collation,
        on_update=actual_col.on_update,
    )
