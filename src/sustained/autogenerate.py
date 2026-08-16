"""
Schema autogeneration: diff the live database against model declarations
and produce a Migration.

diff_schema() introspects tables, columns, primary keys, unique
constraints, foreign keys, defaults, and indexes, then compares them with
the models' tableColumns and indexes declarations. autogenerate() turns
the diff into a Migration:

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

import re
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
    Type,
)

from sustained.dialects import Dialects
from sustained.migrations import Migration
from sustained.schema import build_create_table_sql, render_column_sql

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.model import Model
    from sustained.schema import ColumnDef


class IntrospectedColumn(NamedTuple):
    """One column as reported by the database."""

    raw_type: str
    nullable: bool
    primary_key: bool
    default: Optional[str] = None


class IntrospectedIndex(NamedTuple):
    """One index as reported by the database."""

    columns: Tuple[str, ...]
    unique: bool


# Defaults for tables introspected without keys or indexes. A NamedTuple
# shares one default object across every instance, so these are read-only
# to keep one table's empty mapping from ever becoming another's.
_NO_FOREIGN_KEYS: Mapping[str, str] = MappingProxyType({})
_NO_INDEXES: Mapping[str, IntrospectedIndex] = MappingProxyType({})


class IntrospectedTable(NamedTuple):
    """One table as reported by the database."""

    columns: Dict[str, IntrospectedColumn]
    primary_key: Tuple[str, ...] = ()
    foreign_keys: Mapping[str, str] = _NO_FOREIGN_KEYS
    indexes: Mapping[str, IntrospectedIndex] = _NO_INDEXES


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
    "STRING": "TEXT",
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

_TYPE_PARAMS_RE = re.compile(r"\s*\((.*)\)\s*$")


def normalize_type(raw: str) -> str:
    """
    Reduces an engine type spelling to a logical type name, dropping length
    and precision parameters. Unknown spellings return uppercased as-is.
    """
    base = _TYPE_PARAMS_RE.sub("", raw).strip().upper()
    return _TYPE_SYNONYMS.get(base, base)


def _type_params(raw: str) -> Optional[str]:
    """Extracts '(120)' style parameters from a type spelling, normalized."""
    match = _TYPE_PARAMS_RE.search(raw)
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(1)).upper()


def normalize_default(raw: Optional[str]) -> Optional[str]:
    """
    Reduces a reported column default to a comparable form: strips
    parentheses, Postgres ::type casts, and quotes, and uppercases.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    value = re.sub(r"::[a-zA-Z_ ]+", "", value)
    value = value.strip("'\"")
    return value.upper()


def introspect_schema(
    connection: Any, dialect: Dialects = Dialects.DEFAULT
) -> Dict[str, IntrospectedTable]:
    """
    Reads tables, columns, primary keys, unique constraints, foreign keys,
    defaults, and indexes from the database. The default dialect reads
    SQLite's PRAGMA tables; other dialects read information_schema and
    degrade to column-only data when constraint views are unavailable.
    Names are keyed lowercase.
    """
    if dialect == Dialects.DEFAULT:
        return _introspect_sqlite(connection)
    return _introspect_information_schema(connection, dialect)


def _introspect_sqlite(connection: Any) -> Dict[str, IntrospectedTable]:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    )
    tables = [row[0] for row in cursor.fetchall()]
    schema: Dict[str, IntrospectedTable] = {}
    for table in tables:
        columns: Dict[str, IntrospectedColumn] = {}
        primary_key: List[str] = []
        cursor.execute(f"PRAGMA table_info({table})")
        for _, name, raw_type, notnull, default, pk in cursor.fetchall():
            columns[name.lower()] = IntrospectedColumn(
                raw_type=raw_type or "",
                nullable=not notnull,
                primary_key=bool(pk),
                default=default,
            )
            if pk:
                primary_key.append(name.lower())

        foreign_keys: Dict[str, str] = {}
        cursor.execute(f"PRAGMA foreign_key_list({table})")
        for row in cursor.fetchall():
            _, _, ref_table, from_col, to_col = row[0], row[1], row[2], row[3], row[4]
            foreign_keys[from_col.lower()] = f"{ref_table}.{to_col}".lower()

        indexes: Dict[str, IntrospectedIndex] = {}
        cursor.execute(f"PRAGMA index_list({table})")
        for row in cursor.fetchall():
            index_name, unique, origin = row[1], bool(row[2]), row[3]
            if origin == "pk":
                continue
            cursor.execute(f"PRAGMA index_info({index_name})")
            index_columns = tuple(r[2].lower() for r in cursor.fetchall())
            indexes[index_name.lower()] = IntrospectedIndex(index_columns, unique)

        schema[table.lower()] = IntrospectedTable(
            columns=columns,
            primary_key=tuple(primary_key),
            foreign_keys=foreign_keys,
            indexes=indexes,
        )
    return schema


# System schemas excluded from information_schema introspection.
_SYSTEM_SCHEMAS = (
    "'information_schema'",
    "'pg_catalog'",
    "'sys'",
    "'INFORMATION_SCHEMA'",
)


def _introspect_information_schema(
    connection: Any, dialect: Dialects
) -> Dict[str, IntrospectedTable]:
    cursor = connection.cursor()
    schema_filter = f"table_schema NOT IN ({', '.join(_SYSTEM_SCHEMAS)})"

    columns_by_table: Dict[str, Dict[str, IntrospectedColumn]] = {}
    cursor.execute(
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        f"FROM information_schema.columns WHERE {schema_filter} "
        "ORDER BY table_name, ordinal_position"
    )
    for table, name, data_type, is_nullable, default in cursor.fetchall():
        columns_by_table.setdefault(table.lower(), {})[name.lower()] = (
            IntrospectedColumn(
                raw_type=data_type or "",
                nullable=str(is_nullable).upper() == "YES",
                primary_key=False,
                default=default,
            )
        )

    primary_keys: Dict[str, List[str]] = {}
    unique_indexes: Dict[str, Dict[str, IntrospectedIndex]] = {}
    foreign_keys: Dict[str, Dict[str, str]] = {}
    try:
        cursor.execute(
            "SELECT tc.table_name, tc.constraint_type, tc.constraint_name, "
            "kcu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_name = kcu.table_name "
            f"WHERE tc.{schema_filter} "
            "ORDER BY kcu.ordinal_position"
        )
        constraint_columns: Dict[Tuple[str, str, str], List[str]] = {}
        for table, ctype, cname, column in cursor.fetchall():
            key = (table.lower(), ctype.upper(), cname.lower())
            constraint_columns.setdefault(key, []).append(column.lower())
        for (table, ctype, cname), cols in constraint_columns.items():
            if ctype == "PRIMARY KEY":
                primary_keys[table] = cols
            elif ctype == "UNIQUE":
                unique_indexes.setdefault(table, {})[cname] = IntrospectedIndex(
                    tuple(cols), True
                )
            elif ctype == "FOREIGN KEY":
                for col in cols:
                    # The referenced table is engine-specific to resolve;
                    # presence is enough for constraint notes.
                    foreign_keys.setdefault(table, {})[col] = "?"
    except Exception:
        # The engine does not expose constraint views; degrade to columns.
        pass

    schema: Dict[str, IntrospectedTable] = {}
    for table, columns in columns_by_table.items():
        pk = tuple(primary_keys.get(table, ()))
        for pk_col in pk:
            if pk_col in columns:
                columns[pk_col] = columns[pk_col]._replace(primary_key=True)
        schema[table] = IntrospectedTable(
            columns=columns,
            primary_key=pk,
            foreign_keys=foreign_keys.get(table, {}),
            indexes=unique_indexes.get(table, {}),
        )
    return schema


def _column_shape(column: IntrospectedColumn) -> Tuple[str, Optional[str], bool, bool]:
    """
    One column reduced to the parts two snapshots are compared on: the
    logical type, its parameters, nullability, and key membership. The
    default is left out, since engines report a default they generated
    themselves in spellings that differ between an original column and a
    rebuilt one.
    """
    return (
        normalize_type(column.raw_type),
        _type_params(column.raw_type),
        column.nullable,
        column.primary_key,
    )


def _describe_column(column: IntrospectedColumn) -> str:
    """A column shape in one readable phrase."""
    text = (column.raw_type or "?").upper()
    if not column.nullable:
        text += " NOT NULL"
    if column.primary_key:
        text += " PRIMARY KEY"
    return text


def diff_snapshots(
    before: Dict[str, IntrospectedTable], after: Dict[str, IntrospectedTable]
) -> List[str]:
    """
    Compares two introspected schemas and returns one line per difference,
    empty when they match. A rehearsal uses it to check that the down
    steps put the schema back where it started: `before` is the snapshot
    taken first, `after` is the schema once the down steps have run.

    Tables and columns are compared. Indexes, constraints, and defaults
    are not: their reported spellings differ enough between engines that
    a difference here would report noise as a failure.
    """
    lines: List[str] = []
    for table in sorted(set(after) - set(before)):
        lines.append(f"table '{table}' left behind")
    for table in sorted(set(before) - set(after)):
        lines.append(f"table '{table}' missing")
    for table in sorted(set(before) & set(after)):
        old, new = before[table].columns, after[table].columns
        for column in sorted(set(new) - set(old)):
            lines.append(f"column '{table}.{column}' left behind")
        for column in sorted(set(old) - set(new)):
            lines.append(f"column '{table}.{column}' missing")
        for column in sorted(set(old) & set(new)):
            if _column_shape(old[column]) != _column_shape(new[column]):
                lines.append(
                    f"column '{table}.{column}' changed: "
                    f"{_describe_column(old[column])} became "
                    f"{_describe_column(new[column])}"
                )
    return lines


class SchemaDiff:
    """The differences between declared models and the live database."""

    def __init__(self) -> None:
        self.missing_tables: List[Type["Model"]] = []
        self.new_columns: List[Tuple[Type["Model"], str, "ColumnDef"]] = []
        self.extra_tables: List[str] = []
        self.extra_columns: List[Tuple[str, str]] = []
        self.changed_columns: List[Tuple[str, str, str, str]] = []
        self.new_indexes: List[Tuple[Type["Model"], Any]] = []
        self.extra_indexes: List[Tuple[str, str, IntrospectedIndex]] = []
        self.changed_indexes: List[Tuple[Type["Model"], Any, IntrospectedIndex]] = []
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
            or self.constraint_notes
        )

    def outstanding(self) -> List[str]:
        """
        The differences a generated migration was supposed to close, one
        readable line each, empty when the models all landed.

        Only objects the models declare are reported. A table, column, or
        index the database holds and the models do not is left out: a
        generated migration leaves those alone unless drops are allowed,
        so counting them would report a schema built partly by hand as a
        failure.
        """
        lines: List[str] = []
        for model in self.missing_tables:
            lines.append(f"table '{model.tableName}' was not created")
        for model, name, _ in self.new_columns:
            lines.append(f"column '{model.tableName}.{name}' was not added")
        for table, name, actual, expected in self.changed_columns:
            lines.append(
                f"column '{table}.{name}' is {actual}, the models declare {expected}"
            )
        for model, index in self.new_indexes:
            lines.append(f"index '{index.name}' on '{model.tableName}' was not created")
        for model, index, _ in self.changed_indexes:
            lines.append(f"index '{index.name}' on '{model.tableName}' was not rebuilt")
        return lines

    def summary(self) -> str:
        """A human-readable description of every difference."""
        lines: List[str] = []
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
            (new_key if col == old_key else col): target
            for col, target in old_table.foreign_keys.items()
        }
        actual[table_key] = old_table._replace(
            primary_key=tuple(
                new_key if c == old_key else c for c in old_table.primary_key
            ),
            foreign_keys=renamed_fks,
            indexes=renamed_indexes,
        )


def diff_schema(
    connection: Any,
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
        type_changed = normalize_type(expected_rendered) != normalize_type(
            actual_col.raw_type
        )
        if not type_changed:
            expected_params = _type_params(expected_rendered)
            actual_params = _type_params(actual_col.raw_type)
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
            actual_fk = actual_table.foreign_keys.get(name.lower())
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
    compiler: "Compiler", model: Type["Model"], actual_table: IntrospectedTable
) -> List[str]:
    """
    Rebuilds a SQLite table to match the model: create a new table from the
    declaration, copy rows across, replace the old table, and recreate the
    model's indexes.
    """
    assert model.tableColumns is not None and model.tableName is not None
    table = model.tableName
    temp = f"{table}_sustained_new"
    steps = [build_create_table_sql(compiler, temp, model.tableColumns)]

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

    steps.append(
        f"INSERT INTO {temp} ({', '.join(insert_columns)}) "
        f"SELECT {', '.join(select_parts)} FROM {table}"
    )
    steps.append(f"DROP TABLE {table}")
    steps.append(compiler.compile_rename_table(temp, table))
    steps.extend(model.create_indexes_sql())
    return steps


def autogenerate(
    connection: Any,
    models: List[Type["Model"]],
    id: str,
    dialect: Dialects = Dialects.DEFAULT,
    allow_drops: bool = False,
    ignore_changed_columns: bool = False,
    exclude_tables: Tuple[str, ...] = ("sustained_migrations",),
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    type_casts: Optional[Dict[str, str]] = None,
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

    if (diff.extra_tables or diff.extra_columns or diff.extra_indexes) and (
        not allow_drops
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

    for model in diff.missing_tables:
        assert model.tableColumns is not None
        up_steps.extend(model.create_table_statements())
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
            type_changed = normalize_type(expected_type) != normalize_type(
                actual_col.raw_type
            ) or (_type_params(expected_type) or "") != (
                _type_params(actual_col.raw_type) or _type_params(expected_type) or ""
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
            continue
        column_sql = render_column_sql(compiler, name, coldef, inline_pk=False)
        up_steps.append(compiler.compile_add_column(table_sql, column_sql))
        down_steps.insert(0, compiler.compile_drop_column(table_sql, name))

    # Table rebuilds for SQLite consume every remaining change on the table.
    for table_key, model in rebuild_tables.items():
        up_steps.extend(_sqlite_rebuild_steps(compiler, model, actual[table_key]))
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

    if not up_steps:
        return None
    return Migration(
        id=id, up=up_steps, down=down_steps if reversible and down_steps else None
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
    )
