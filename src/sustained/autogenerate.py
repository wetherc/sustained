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
  reversible. Dropping extra indexes also requires allow_drops=True but
  reverses, since the index definition is known.
- Primary key, foreign key, column-level unique, and default differences
  are reported in the diff's constraint notes but never auto-migrated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple, Type

from sustained.dialects import Dialects
from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
    async_introspect_schema,
    diff_snapshots,
    introspect_schema,
    normalize_default,
    normalize_type,
    parse_inline_enum,
    type_params,
)
from sustained.migrations import Migration
from sustained.schema import (
    build_create_table_sql,
    collect_enum_types,
    enum_check_constraint_sql,
    render_column_sql,
)
from sustained.types import Connection

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.model import Model
    from sustained.schema import ColumnDef, Index

# Reading a schema moved to sustained.introspect. The names stay importable
# from here, where callers have always found them.
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


class SchemaDiff:
    """The differences between declared models and the live database."""

    def __init__(self) -> None:
        self.missing_tables: List[Type["Model"]] = []
        self.new_columns: List[Tuple[Type["Model"], str, "ColumnDef"]] = []
        self.extra_tables: List[str] = []
        self.extra_columns: List[Tuple[str, str]] = []
        self.changed_columns: List[Tuple[str, str, str, str]] = []
        self.new_indexes: List[Tuple[Type["Model"], "Index"]] = []
        self.extra_indexes: List[Tuple[str, str, IntrospectedIndex]] = []
        self.changed_indexes: List[Tuple[Type["Model"], "Index", IntrospectedIndex]] = (
            []
        )
        self.new_enum_types: List[Tuple[str, Tuple[str, ...]]] = []
        self.changed_enum_types: List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = []
        self.constraint_notes: List[str] = []

    def is_empty(self) -> bool:
        return not (
            self.missing_tables
            or self.new_columns
            or self.extra_tables
            or self.extra_columns
            or self.changed_columns
            or self.new_indexes
            or self.extra_indexes
            or self.changed_indexes
            or self.new_enum_types
            or self.changed_enum_types
            or self.constraint_notes
        )

    def outstanding(self, ignore_changed_columns: bool = False) -> List[str]:
        """
        The differences a generated migration was supposed to close, one
        readable line each, empty when the models all landed.

        Only objects the models declare are reported. A table, column, or
        index the database holds and the models do not is left out: a
        generated migration leaves those alone unless drops are allowed,
        so counting them would report a schema built partly by hand as a
        failure.

        Pass ignore_changed_columns=True to leave type and nullability
        changes out, matching a migration generated with the same option:
        those columns were never meant to be closed.
        """
        lines: List[str] = []
        for model in self.missing_tables:
            lines.append(f"table '{model.tableName}' was not created")
        for model, name, _ in self.new_columns:
            lines.append(f"column '{model.tableName}.{name}' was not added")
        if not ignore_changed_columns:
            for table, name, actual, expected in self.changed_columns:
                lines.append(
                    f"column '{table}.{name}' is {actual}, "
                    f"the models declare {expected}"
                )
        for model, index in self.new_indexes:
            lines.append(f"index '{index.name}' on '{model.tableName}' was not created")
        for model, index, _ in self.changed_indexes:
            lines.append(f"index '{index.name}' on '{model.tableName}' was not rebuilt")
        for type_name, _ in self.new_enum_types:
            lines.append(f"enum type '{type_name}' was not created")
        for type_name, live_values, declared_values in self.changed_enum_types:
            lines.append(
                f"enum type '{type_name}' has values "
                f"({', '.join(live_values)}), the models declare "
                f"({', '.join(declared_values)})"
            )
        return lines

    def summary(self) -> str:
        """A human-readable description of every difference."""
        lines: List[str] = []
        for type_name, _ in self.new_enum_types:
            lines.append(f"create enum type {type_name}")
        for type_name, live_values, declared_values in self.changed_enum_types:
            additions = _enum_value_additions(live_values, declared_values)
            if additions is not None:
                for value in additions:
                    lines.append(f"add value '{value}' to enum type {type_name}")
            else:
                lines.append(
                    f"change enum type {type_name}: database has "
                    f"({', '.join(live_values)}), model declares "
                    f"({', '.join(declared_values)})"
                )
        for model in self.missing_tables:
            lines.append(f"create table {model.tableName}")
        for model, name, _ in self.new_columns:
            lines.append(f"add column {model.tableName}.{name}")
        for model, index in self.new_indexes:
            lines.append(f"create index {index.name} on {model.tableName}")
        for model, index, _ in self.changed_indexes:
            lines.append(f"rebuild index {index.name} on {model.tableName}")
        for table in self.extra_tables:
            lines.append(f"drop table {table} (destructive)")
        for table, name in self.extra_columns:
            lines.append(f"drop column {table}.{name} (destructive)")
        for table, name, _ in self.extra_indexes:
            lines.append(f"drop index {name} on {table}")
        for table, name, actual, expected in self.changed_columns:
            lines.append(
                f"change column {table}.{name}: database has {actual}, "
                f"model declares {expected}"
            )
        for note in self.constraint_notes:
            lines.append(f"note: {note} (not auto-migrated)")
        return "\n".join(lines) if lines else "schema up to date"


def _enum_value_additions(
    actual: Tuple[str, ...], expected: Tuple[str, ...]
) -> Optional[Tuple[str, ...]]:
    """
    The values the models append to an enum type's existing list, or None
    when the change is not a pure append. Postgres adds a value in place
    but never removes or reorders one, so only an appended tail can be
    generated.
    """
    if len(expected) > len(actual) and expected[: len(actual)] == actual:
        return expected[len(actual) :]
    return None


def _declared_enum_types(
    models: List[Type["Model"]],
) -> Dict[str, Tuple[str, ...]]:
    """
    Every enum type the models declare, name to value tuple, merged
    across models. The same name declared with different values in two
    models raises: it would be one database object with two definitions.
    """
    types: Dict[str, Tuple[str, ...]] = {}
    for model in models:
        for type_name, values in collect_enum_types(model.tableColumns or {}).items():
            known = types.get(type_name)
            if known is not None and known != values:
                raise ValueError(
                    f"Enum '{type_name}' is declared with different "
                    f"values in two models: {', '.join(known)} versus "
                    f"{', '.join(values)}."
                )
            types[type_name] = values
    return types


def _diff_enum_types(
    diff: SchemaDiff,
    declared: Dict[str, Type["Model"]],
    declared_types: Dict[str, Tuple[str, ...]],
    actual: Snapshot,
) -> None:
    """
    Compares the models' enum types against the database, on dialects
    where an enum is a named type object. With a catalog read (Postgres),
    absence and value changes come straight from it. Without one
    (DuckDB), a type is taken as present when a column of it already
    exists, and its live values are read from the column's own inline
    type spelling when the engine writes one.
    """
    if actual.enum_types_read:
        for type_name, values in declared_types.items():
            existing = actual.enum_types.get(type_name.lower())
            if existing is None:
                diff.new_enum_types.append((type_name, values))
            elif existing != values:
                diff.changed_enum_types.append((type_name, existing, values))
        return
    present: Set[str] = set()
    changed: Set[str] = set()
    for table_key, model in declared.items():
        actual_table = actual.get(table_key)
        if actual_table is None:
            continue
        for name, coldef in (model.tableColumns or {}).items():
            if coldef.type_name != "ENUM":
                continue
            actual_col = actual_table.columns.get(name.lower())
            if actual_col is None or not _actual_column_is_enum(actual_col, coldef):
                continue
            assert coldef.enum_name is not None and coldef.enum_values is not None
            present.add(coldef.enum_name.lower())
            live_values = actual_col.enum_values or parse_inline_enum(
                actual_col.raw_type
            )
            if (
                live_values
                and live_values != coldef.enum_values
                and coldef.enum_name.lower() not in changed
            ):
                changed.add(coldef.enum_name.lower())
                diff.changed_enum_types.append(
                    (coldef.enum_name, tuple(live_values), coldef.enum_values)
                )
    for type_name, values in declared_types.items():
        if type_name.lower() not in present:
            diff.new_enum_types.append((type_name, values))


def _actual_column_is_enum(actual_col: IntrospectedColumn, coldef: "ColumnDef") -> bool:
    """
    Whether the live column already holds the declared enum type. The
    catalog says so directly when it names enum types; otherwise the
    column's raw type either is the type's own name or spells the value
    list inline, as DuckDB's information_schema does.
    """
    assert coldef.enum_name is not None
    if actual_col.enum_name is not None:
        return actual_col.enum_name == coldef.enum_name.lower()
    if actual_col.raw_type.lower().strip('"`[]') == coldef.enum_name.lower():
        return True
    return bool(parse_inline_enum(actual_col.raw_type))


def _apply_renames(
    actual: Dict[str, IntrospectedTable],
    renames: Dict[str, str],
    table_renames: Dict[str, str],
) -> None:
    """
    Rewrites the introspected schema as if the renames had already run, so
    renamed objects do not diff as drop-plus-add.
    """
    for old, new in table_renames.items():
        old_key, new_key = old.lower(), new.lower()
        if old_key not in actual:
            raise ValueError(f"Cannot rename unknown table '{old}'.")
        actual[new_key] = actual.pop(old_key)
    for path, new_name in renames.items():
        if "." not in path:
            raise ValueError(
                f"Column rename keys must be 'table.column', got {path!r}."
            )
        table, old_name = path.rsplit(".", 1)
        table_key = table.lower()
        old_key, new_key = old_name.lower(), new_name.lower()
        if table_key not in actual or old_key not in actual[table_key].columns:
            raise ValueError(f"Cannot rename unknown column '{path}'.")
        old_table = actual[table_key]
        columns = old_table.columns
        columns[new_key] = columns.pop(old_key)
        # Engines rewrite the column name inside indexes, keys, and
        # constraints on rename; mirror that so nothing diffs as changed.
        renamed_indexes = {
            name: IntrospectedIndex(
                tuple(new_key if c == old_key else c for c in index.columns),
                index.unique,
            )
            for name, index in old_table.indexes.items()
        }
        renamed_fks = {
            name: fk._replace(
                columns=tuple(new_key if c == old_key else c for c in fk.columns)
            )
            for name, fk in old_table.foreign_keys.items()
        }
        actual[table_key] = old_table._replace(
            primary_key=tuple(
                new_key if c == old_key else c for c in old_table.primary_key
            ),
            foreign_keys=renamed_fks,
            indexes=renamed_indexes,
        )


def diff_schema(
    connection: Connection,
    models: List[Type["Model"]],
    dialect: Dialects = Dialects.DEFAULT,
    exclude_tables: Tuple[str, ...] = ("sustained_migrations",),
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
) -> SchemaDiff:
    """
    Compares the models' declarations against the live database and
    returns the differences. Rename hints are applied first, so renamed
    objects compare under their new names.
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
        _diff_columns(compiler, diff, model, actual_table)
        _diff_indexes(diff, model, actual_table)
        _diff_constraints(diff, model, actual_table)

    for table_key in actual:
        if table_key not in declared and table_key not in excluded:
            diff.extra_tables.append(table_key)

    return diff


def _diff_columns(
    compiler: "Compiler",
    diff: SchemaDiff,
    model: Type["Model"],
    actual_table: IntrospectedTable,
) -> None:
    assert model.tableColumns is not None
    table_name = model.tableName or ""
    for name, coldef in model.tableColumns.items():
        actual_col = actual_table.columns.get(name.lower())
        if actual_col is None:
            diff.new_columns.append((model, name, coldef))
            continue
        expected_rendered = compiler.compile_column_type(coldef)
        if coldef.type_name == "ENUM" and compiler.enum_strategy() == "native":
            # A native enum column matches on its type's name; value
            # differences live on the type itself and are reported in
            # changed_enum_types, not here.
            type_changed = not _actual_column_is_enum(actual_col, coldef)
        else:
            type_changed = normalize_type(expected_rendered) != normalize_type(
                actual_col.raw_type
            )
            if not type_changed:
                expected_params = type_params(expected_rendered)
                actual_params = type_params(actual_col.raw_type)
                if (
                    expected_params is not None
                    and actual_params is not None
                    and expected_params != actual_params
                ):
                    type_changed = True
        # SQLite reports INTEGER PRIMARY KEY as nullable, so nullability
        # is only compared on non-key columns.
        null_changed = (
            not coldef.primary_key
            and not actual_col.primary_key
            and actual_col.nullable != coldef.nullable
        )
        if type_changed or null_changed:
            expected_desc = expected_rendered.upper() + (
                "" if coldef.nullable else " NOT NULL"
            )
            actual_desc = (actual_col.raw_type or "?").upper() + (
                "" if actual_col.nullable else " NOT NULL"
            )
            diff.changed_columns.append((table_name, name, actual_desc, expected_desc))
    declared_names = {c.lower() for c in model.tableColumns}
    for name in actual_table.columns:
        if name not in declared_names:
            diff.extra_columns.append((table_name, name))


def _diff_indexes(
    diff: SchemaDiff, model: Type["Model"], actual_table: IntrospectedTable
) -> None:
    declared_indexes = {i.name.lower(): i for i in model.indexes or []}
    for name, index in declared_indexes.items():
        actual_index = actual_table.indexes.get(name)
        if actual_index is None:
            diff.new_indexes.append((model, index))
        elif (
            tuple(c.lower() for c in index.columns) != actual_index.columns
            or index.unique != actual_index.unique
        ):
            diff.changed_indexes.append((model, index, actual_index))
    for name, actual_index in actual_table.indexes.items():
        if name in declared_indexes or name.startswith("sqlite_autoindex"):
            continue
        # Unique indexes backing a declared column-level UNIQUE or the
        # primary key are not extras.
        if actual_index.unique and len(actual_index.columns) == 1:
            column = actual_index.columns[0]
            coldef = (model.tableColumns or {}).get(column)
            if coldef is not None and (coldef.unique or coldef.primary_key):
                continue
        diff.extra_indexes.append((model.tableName or "", name, actual_index))


def _diff_constraints(
    diff: SchemaDiff, model: Type["Model"], actual_table: IntrospectedTable
) -> None:
    assert model.tableColumns is not None
    table_name = model.tableName or ""

    expected_pk = tuple(
        sorted(n.lower() for n, c in model.tableColumns.items() if c.primary_key)
    )
    actual_pk = tuple(sorted(actual_table.primary_key))
    if actual_pk and expected_pk != actual_pk:
        diff.constraint_notes.append(
            f"{table_name} primary key is ({', '.join(actual_pk)}), "
            f"model declares ({', '.join(expected_pk)})"
        )

    for name, coldef in model.tableColumns.items():
        actual_col = actual_table.columns.get(name.lower())
        if actual_col is None:
            continue
        if coldef.references is not None:
            actual_fk = actual_table.foreign_key_targets.get(name.lower())
            if actual_fk is None:
                diff.constraint_notes.append(
                    f"{table_name}.{name} declares a foreign key to "
                    f"{coldef.references} that the database does not have"
                )
            elif actual_fk not in ("?", coldef.references.lower()):
                diff.constraint_notes.append(
                    f"{table_name}.{name} foreign key targets {actual_fk}, "
                    f"model declares {coldef.references.lower()}"
                )
        if coldef.unique and not coldef.primary_key:
            covered = any(
                index.unique and index.columns == (name.lower(),)
                for index in actual_table.indexes.values()
            )
            if not covered:
                diff.constraint_notes.append(
                    f"{table_name}.{name} declares UNIQUE but the database "
                    "has no unique index on it"
                )
        expected_default = (
            None if coldef.default is None else normalize_default(str(coldef.default))
        )
        actual_default = normalize_default(actual_col.default)
        if expected_default != actual_default:
            diff.constraint_notes.append(
                f"{table_name}.{name} default is "
                f"{actual_default or 'none'}, model declares "
                f"{expected_default or 'none'}"
            )


def _sqlite_rebuild_steps(
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
    steps = [build_create_table_sql(compiler, temp, model.tableColumns, extras=extras)]

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
        table_sql = compiler.quote_fully_qualified_identifier(table)
        statements.append(
            compiler.compile_create_index(
                name, table_sql, list(index.columns), index.unique
            )
        )
    return statements


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
            those are not a reason to stop.
    """
    compiler = Dialects.get_compiler(dialect)
    renames = renames or {}
    table_renames = table_renames or {}
    type_casts = type_casts or {}
    diff = diff_schema(
        connection, models, dialect, exclude_tables, renames, table_renames
    )
    actual = introspect_schema(connection, dialect)
    _apply_renames(actual, renames, table_renames)
    models_by_table = {m.tableName.lower(): m for m in models if m.tableName}

    if (
        (diff.extra_tables or diff.extra_columns or diff.extra_indexes)
        and not allow_drops
        and not ignore_undeclared
    ):
        dropped = (
            list(diff.extra_tables)
            + [f"{t}.{c}" for t, c in diff.extra_columns]
            + [f"index {n}" for _, n, _ in diff.extra_indexes]
        )
        raise ValueError(
            "The database has objects the models do not declare: "
            f"{', '.join(dropped)}. Pass allow_drops=True to generate the "
            "drops, or add them to exclude_tables."
        )

    up_steps: List[str] = []
    down_steps: List[str] = []
    reversible = True
    rebuild_tables: Dict[str, Type["Model"]] = {}

    # Renames first, so later steps address the new names.
    for old, new in table_renames.items():
        old_sql = compiler.quote_fully_qualified_identifier(old)
        new_sql = compiler.quote_fully_qualified_identifier(new)
        up_steps.append(compiler.compile_rename_table(old_sql, new_sql))
        down_steps.insert(0, compiler.compile_rename_table(new_sql, old_sql))
    for path, new_name in renames.items():
        table, old_name = path.rsplit(".", 1)
        table_sql = compiler.quote_fully_qualified_identifier(table)
        up_steps.append(compiler.compile_rename_column(table_sql, old_name, new_name))
        down_steps.insert(
            0, compiler.compile_rename_column(table_sql, new_name, old_name)
        )

    # Enum types first, before any table or column that references them.
    # New types are created; declared values that extend the database's
    # list are appended with ADD VALUE, which no engine takes back, so
    # such a migration has no down. Any other value change cannot run in
    # place and refuses with the recipe.
    created_enum_types: List[str] = []
    for type_name, values in diff.new_enum_types:
        up_steps.append(compiler.compile_create_enum_type(type_name, list(values)))
        created_enum_types.append(type_name)
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
            up_steps.append(compiler.compile_add_enum_value(type_name, value))
        reversible = False

    for model in diff.missing_tables:
        assert model.tableColumns is not None
        up_steps.extend(model.create_table_statements(include_enum_types=False))
        down_steps.insert(0, model.drop_table_sql())

    # Changed columns: ALTER in place where the dialect can, otherwise
    # mark the table for a rebuild.
    if not ignore_changed_columns:
        for table, name, actual_desc, expected_desc in diff.changed_columns:
            model = models_by_table[table.lower()]
            assert model.tableColumns is not None
            coldef = model.tableColumns[name]
            actual_col = actual[table.lower()].columns[name.lower()]
            if not compiler.supports_alter_column():
                rebuild_tables[table.lower()] = model
                continue
            table_sql = model._qualified_table_sql()
            expected_type = compiler.compile_column_type(coldef)
            if coldef.type_name == "ENUM" and compiler.enum_strategy() == "native":
                type_changed = not _actual_column_is_enum(actual_col, coldef)
            else:
                type_changed = normalize_type(expected_type) != normalize_type(
                    actual_col.raw_type
                ) or (type_params(expected_type) or "") != (
                    type_params(actual_col.raw_type) or type_params(expected_type) or ""
                )
            if type_changed:
                using = type_casts.get(f"{table}.{name}")
                up_steps.extend(
                    compiler.compile_alter_column_type(
                        table_sql, name, expected_type, using
                    )
                )
                for statement in reversed(
                    compiler.compile_alter_column_type(
                        table_sql, name, actual_col.raw_type
                    )
                ):
                    down_steps.insert(0, statement)
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
                    quoted = compiler.quote_identifier(name)
                    up_steps.append(
                        f"UPDATE {table_sql} SET {quoted} = "
                        f"{compiler.format_value(filler)} WHERE {quoted} IS NULL"
                    )
                up_steps.extend(
                    compiler.compile_alter_column_nullability(
                        table_sql, name, expected_type, coldef.nullable
                    )
                )
                for statement in reversed(
                    compiler.compile_alter_column_nullability(
                        table_sql, name, expected_type, actual_col.nullable
                    )
                ):
                    down_steps.insert(0, statement)

    # An enum column held by a CHECK constraint needs the constraint
    # added beside the new column. A dialect that cannot alter
    # constraints in place (SQLite) rebuilds the table instead; the
    # rebuilt CREATE TABLE carries the constraint.
    if compiler.enum_strategy() == "check" and not compiler.supports_alter_column():
        for model, _, coldef in diff.new_columns:
            if coldef.type_name == "ENUM":
                rebuild_tables[(model.tableName or "").lower()] = model

    # New columns on tables that are not being rebuilt.
    for model, name, coldef in diff.new_columns:
        table_key = (model.tableName or "").lower()
        if table_key in rebuild_tables:
            continue
        if coldef.primary_key or coldef.autoincrement:
            raise ValueError(
                f"Cannot add '{model.tableName}.{name}' with ALTER TABLE: "
                "primary key and autoincrement columns need a hand-written "
                "migration."
            )
        table_sql = model._qualified_table_sql()
        if not coldef.nullable and coldef.default is None:
            if coldef.backfill is None:
                raise ValueError(
                    f"Cannot add NOT NULL column '{model.tableName}.{name}' "
                    "without a default or backfill; existing rows would "
                    "have no value."
                )
            if not compiler.supports_alter_column():
                rebuild_tables[table_key] = model
                continue
            # Add nullable, backfill, then tighten.
            relaxed = render_column_sql(
                compiler,
                name,
                _relaxed_copy(coldef),
                inline_pk=False,
            )
            quoted = compiler.quote_identifier(name)
            up_steps.append(compiler.compile_add_column(table_sql, relaxed))
            up_steps.append(
                f"UPDATE {table_sql} SET {quoted} = "
                f"{compiler.format_value(coldef.backfill)} WHERE {quoted} IS NULL"
            )
            up_steps.extend(
                compiler.compile_alter_column_nullability(
                    table_sql, name, compiler.compile_column_type(coldef), False
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
        up_steps.append(compiler.compile_add_column(table_sql, column_sql))
        down_steps.insert(0, compiler.compile_drop_column(table_sql, name))
        _add_enum_check(compiler, up_steps, down_steps, table_sql, model, name, coldef)
        _add_foreign_key(compiler, up_steps, down_steps, table_sql, model, name, coldef)

    # Table rebuilds for SQLite consume every remaining change on the table.
    for table_key, model in rebuild_tables.items():
        up_steps.extend(
            _sqlite_rebuild_steps(compiler, model, actual[table_key], allow_drops)
        )
        reversible = False

    # Index changes.
    for model, index in diff.new_indexes:
        table_sql = model._qualified_table_sql()
        up_steps.append(
            compiler.compile_create_index(
                index.name, table_sql, list(index.columns), index.unique
            )
        )
        down_steps.insert(0, compiler.compile_drop_index(index.name, table_sql))
    for model, index, actual_index in diff.changed_indexes:
        table_sql = model._qualified_table_sql()
        up_steps.append(compiler.compile_drop_index(index.name, table_sql))
        up_steps.append(
            compiler.compile_create_index(
                index.name, table_sql, list(index.columns), index.unique
            )
        )
        down_steps.insert(
            0,
            compiler.compile_create_index(
                index.name, table_sql, list(actual_index.columns), actual_index.unique
            ),
        )
        down_steps.insert(0, compiler.compile_drop_index(index.name, table_sql))

    if allow_drops:
        for table, name, actual_index in diff.extra_indexes:
            table_sql = compiler.quote_fully_qualified_identifier(table)
            up_steps.append(compiler.compile_drop_index(name, table_sql))
            down_steps.insert(
                0,
                compiler.compile_create_index(
                    name, table_sql, list(actual_index.columns), actual_index.unique
                ),
            )
        for table, name in diff.extra_columns:
            table_sql = compiler.quote_fully_qualified_identifier(table)
            if table.lower() in rebuild_tables:
                continue
            up_steps.append(compiler.compile_drop_column(table_sql, name))
            reversible = False
        for table in diff.extra_tables:
            table_sql = compiler.quote_fully_qualified_identifier(table)
            up_steps.append(f"DROP TABLE {table_sql}")
            reversible = False

    # Types created in this migration drop last on the way down, after
    # every table that referenced them is gone.
    for type_name in created_enum_types:
        down_steps.append(compiler.compile_drop_enum_type(type_name))

    if not up_steps:
        return None
    return Migration(
        id=id, up=up_steps, down=down_steps if reversible and down_steps else None
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
        compiler.compile_add_foreign_key(
            table_sql,
            constraint,
            name,
            compiler.quote_fully_qualified_identifier(ref_table),
            ref_column,
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
    up_steps.append(f"ALTER TABLE {table_sql} ADD {constraint_sql}")
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
