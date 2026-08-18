"""
Reading a live schema, and comparing two reads.

introspect_schema() and async_introspect_schema() report the tables,
columns, primary keys, unique constraints, foreign keys, defaults, and
indexes a database currently holds. Both drive the same generator-based
plan, so one dialect's reading code serves a blocking connection and an
async adapter alike.

diff_snapshots() compares two such reads. A rehearsal uses it to check
that the down steps put the schema back where it started.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Dict,
    Generator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    cast,
)

from sustained.dialects import Dialects
from sustained.types import Connection, RowValue

if TYPE_CHECKING:
    from sustained.aio import AsyncAdapter


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
    # MySQL sizes its text columns in the type name. TINYINT is left off
    # this table on purpose: TINYINT(1) is how MySQL spells a boolean, and
    # folding plain TINYINT into INTEGER would make the two the same
    # column to a diff.
    "TINYTEXT": "TEXT",
    "MEDIUMTEXT": "TEXT",
    "LONGTEXT": "TEXT",
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


def type_params(raw: str) -> Optional[str]:
    """Extracts '(120)' style parameters from a type spelling, normalized."""
    match = _TYPE_PARAMS_RE.search(raw)
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(1)).upper()


def normalize_default(raw: Optional[str]) -> Optional[str]:
    """
    Reduces a reported column default to a comparable form: strips
    parentheses, Postgres ::type casts, quotes, and an empty argument
    list, and uppercases. The argument list is why MariaDB's
    current_timestamp() and MySQL's CURRENT_TIMESTAMP compare equal.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    value = re.sub(r"::[a-zA-Z_ ]+", "", value)
    value = value.strip("'\"")
    value = re.sub(r"\(\s*\)$", "", value.strip())
    return value.upper()


# A schema read expressed as a sequence of queries. The plan yields one
# statement at a time and receives its rows back, so the same reading
# code serves a blocking connection and an async adapter. A statement
# that fails is thrown back in, and the plan decides whether to degrade
# or give up.
SchemaPlan = Generator[str, List[Sequence[RowValue]], Dict[str, IntrospectedTable]]


def _finished(stop: StopIteration) -> Dict[str, IntrospectedTable]:
    """The schema a finished plan carried on its StopIteration."""
    return cast(Dict[str, IntrospectedTable], stop.value)


def introspect_schema(
    connection: Connection, dialect: Dialects = Dialects.DEFAULT
) -> Dict[str, IntrospectedTable]:
    """
    Reads tables, columns, primary keys, unique constraints, foreign keys,
    defaults, and indexes from the database. The default dialect reads
    SQLite's PRAGMA tables. Postgres reads information_schema together
    with pg_index, so every index is visible, varchar lengths and numeric
    precision survive, enum columns report their type's name, and foreign
    key targets resolve. Other dialects read plain information_schema and
    degrade to column-only data when constraint views are unavailable.
    Names are keyed lowercase.
    """
    plan = _schema_plan(dialect)
    cursor = connection.cursor()

    def run(sql: str) -> List[Sequence[RowValue]]:
        cursor.execute(sql)
        return list(cursor.fetchall())

    sql = next(plan)
    while True:
        try:
            rows = run(sql)
        except Exception as error:
            try:
                sql = plan.throw(error)
            except StopIteration as stop:
                return _finished(stop)
            continue
        try:
            sql = plan.send(rows)
        except StopIteration as stop:
            return _finished(stop)


async def async_introspect_schema(
    adapter: "AsyncAdapter", dialect: Dialects = Dialects.DEFAULT
) -> Dict[str, IntrospectedTable]:
    """
    Reads the schema through an async adapter, returning what
    introspect_schema() returns. Both run the same reading code, so a
    dialect behaves the same on either path.
    """
    plan = _schema_plan(dialect)
    sql = next(plan)
    while True:
        try:
            _, rows = await adapter.fetch(sql, ())
        except Exception as error:
            try:
                sql = plan.throw(error)
            except StopIteration as stop:
                return _finished(stop)
            continue
        try:
            sql = plan.send(list(rows))
        except StopIteration as stop:
            return _finished(stop)


def _schema_plan(dialect: Dialects) -> SchemaPlan:
    if dialect == Dialects.DEFAULT:
        return _sqlite_plan()
    if dialect == Dialects.MYSQL:
        return _mysql_plan()
    if dialect == Dialects.POSTGRES:
        return _postgres_plan()
    return _information_schema_plan()


def _sqlite_plan() -> SchemaPlan:
    rows = yield (
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    )
    tables = [row[0] for row in rows]
    schema: Dict[str, IntrospectedTable] = {}
    for table in tables:
        columns: Dict[str, IntrospectedColumn] = {}
        primary_key: List[str] = []
        for _, name, raw_type, notnull, default, pk in (
            yield f"PRAGMA table_info({table})"
        ):
            columns[name.lower()] = IntrospectedColumn(
                raw_type=raw_type or "",
                nullable=not notnull,
                primary_key=bool(pk),
                default=default,
            )
            if pk:
                primary_key.append(name.lower())

        foreign_keys: Dict[str, str] = {}
        for row in (yield f"PRAGMA foreign_key_list({table})"):
            ref_table, from_col, to_col = row[2], row[3], row[4]
            foreign_keys[from_col.lower()] = f"{ref_table}.{to_col}".lower()

        indexes: Dict[str, IntrospectedIndex] = {}
        index_rows = yield f"PRAGMA index_list({table})"
        for row in index_rows:
            index_name, unique, origin = row[1], bool(row[2]), row[3]
            if origin == "pk":
                continue
            info = yield f"PRAGMA index_info({index_name})"
            names = [r[2] for r in info]
            if any(name is None for name in names):
                # An expression index reports NULL column names. It cannot
                # be compared against a model's column list, so it is left
                # out of the schema rather than crashing the read.
                continue
            index_columns = tuple(name.lower() for name in names)
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


class Catalog(NamedTuple):
    """
    How one engine's information_schema differs from the shared read.

    Attributes:
        schema_filter: The WHERE fragment that picks the schemas to read.
        type_column: The column holding the type spelling to compare on.
        join_on_schema: Whether the constraint join must match schemas as
            well as names, on engines where a constraint name is only
            unique within its schema.
    """

    schema_filter: str
    type_column: str
    join_on_schema: bool


ANSI_CATALOG = Catalog(
    schema_filter=f"table_schema NOT IN ({', '.join(_SYSTEM_SCHEMAS)})",
    type_column="data_type",
    join_on_schema=False,
)

MYSQL_CATALOG = Catalog(
    # A MySQL schema is a database, and every other database on the server
    # belongs to someone else. DATABASE() is the one the connection is on.
    schema_filter="table_schema = DATABASE()",
    # data_type reports 'varchar' and keeps the length in a column of its
    # own. column_type reports 'varchar(120)', which is what the compiler
    # emits, so a column never drifts against its own DDL.
    type_column="column_type",
    join_on_schema=True,
)


def _information_schema_plan(catalog: Catalog = ANSI_CATALOG) -> SchemaPlan:
    schema_filter = catalog.schema_filter

    columns_by_table: Dict[str, Dict[str, IntrospectedColumn]] = {}
    column_rows = yield (
        f"SELECT table_name, column_name, {catalog.type_column}, "
        "is_nullable, column_default "
        f"FROM information_schema.columns WHERE {schema_filter} "
        "ORDER BY table_name, ordinal_position"
    )
    for table, name, data_type, is_nullable, default in column_rows:
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
    schema_join = (
        "AND tc.table_schema = kcu.table_schema " if catalog.join_on_schema else ""
    )
    try:
        constraint_rows = yield (
            "SELECT tc.table_name, tc.constraint_type, tc.constraint_name, "
            "kcu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_name = kcu.table_name "
            f"{schema_join}"
            f"WHERE tc.{schema_filter} "
            "ORDER BY kcu.ordinal_position"
        )
        constraint_columns: Dict[Tuple[str, str, str], List[str]] = {}
        for table, ctype, cname, column in constraint_rows:
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

    schema = {}
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


def _postgres_column_type(
    data_type: str,
    udt_name: Optional[str],
    char_length: Optional[RowValue],
    precision: Optional[RowValue],
    scale: Optional[RowValue],
) -> str:
    """
    The type spelling a Postgres column is compared on. information_schema's
    data_type alone loses too much: a varchar drops its length, a numeric
    its precision, and an enum reads as USER-DEFINED. The parameters go
    back on, and an enum reports its type's own name from udt_name.
    """
    if data_type == "USER-DEFINED" and udt_name:
        return str(udt_name)
    if data_type in ("character varying", "character") and char_length is not None:
        return f"{data_type}({char_length})"
    if data_type == "numeric" and precision is not None:
        return f"numeric({precision},{scale if scale is not None else 0})"
    return data_type


def _postgres_plan() -> SchemaPlan:
    columns_by_table: Dict[str, Dict[str, IntrospectedColumn]] = {}
    column_rows = yield (
        "SELECT c.table_name, c.column_name, c.data_type, c.udt_name, "
        "c.character_maximum_length, c.numeric_precision, c.numeric_scale, "
        "c.is_nullable, c.column_default "
        "FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE c.table_schema NOT IN ('information_schema', 'pg_catalog') "
        "AND t.table_type = 'BASE TABLE' "
        "ORDER BY c.table_name, c.ordinal_position"
    )
    for row in column_rows:
        table, name, data_type, udt_name = (str(v) for v in row[:4])
        char_length, precision, scale, is_nullable, default = row[4:9]
        columns_by_table.setdefault(table.lower(), {})[name.lower()] = (
            IntrospectedColumn(
                raw_type=_postgres_column_type(
                    data_type, udt_name, char_length, precision, scale
                ),
                nullable=str(is_nullable).upper() == "YES",
                primary_key=False,
                default=None if default is None else str(default),
            )
        )

    primary_keys: Dict[str, Tuple[str, ...]] = {}
    indexes: Dict[str, Dict[str, IntrospectedIndex]] = {}
    try:
        index_rows = yield (
            "SELECT t.relname, i.relname, ix.indisunique, ix.indisprimary, "
            "a.attname "
            "FROM pg_catalog.pg_index ix "
            "JOIN pg_catalog.pg_class t ON t.oid = ix.indrelid "
            "JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
            "CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) "
            "LEFT JOIN pg_catalog.pg_attribute a "
            "ON a.attrelid = t.oid AND a.attnum = k.attnum "
            "WHERE t.relkind IN ('r', 'p') "
            "AND n.nspname NOT IN ('information_schema', 'pg_catalog') "
            "ORDER BY t.relname, i.relname, k.ord"
        )
        index_columns: Dict[Tuple[str, str, bool, bool], List[Optional[str]]] = {}
        for table, index, unique, primary, attname in index_rows:
            key = (str(table).lower(), str(index).lower(), bool(unique), bool(primary))
            index_columns.setdefault(key, []).append(
                None if attname is None else str(attname).lower()
            )
        for (table, index, unique, primary), names in index_columns.items():
            if any(name is None for name in names):
                # An expression index has no column name for that key part.
                # It cannot be compared against a model's column list, so it
                # is left out of the schema rather than crashing the read.
                continue
            key_columns = tuple(cast(str, name) for name in names)
            if primary:
                primary_keys[table] = key_columns
            else:
                indexes.setdefault(table, {})[index] = IntrospectedIndex(
                    key_columns, unique
                )
    except Exception:
        # No pg_index to read; degrade to columns without keys or indexes.
        pass

    foreign_keys: Dict[str, Dict[str, str]] = {}
    try:
        fk_rows = yield (
            "SELECT src.table_name, src.column_name, "
            "tgt.table_name, tgt.column_name "
            "FROM information_schema.referential_constraints rc "
            "JOIN information_schema.key_column_usage src "
            "ON src.constraint_schema = rc.constraint_schema "
            "AND src.constraint_name = rc.constraint_name "
            "JOIN information_schema.key_column_usage tgt "
            "ON tgt.constraint_schema = rc.unique_constraint_schema "
            "AND tgt.constraint_name = rc.unique_constraint_name "
            "AND tgt.ordinal_position = src.position_in_unique_constraint "
            "WHERE rc.constraint_schema NOT IN ('information_schema', 'pg_catalog') "
            "ORDER BY rc.constraint_name, src.ordinal_position"
        )
        for table, column, ref_table, ref_column in fk_rows:
            foreign_keys.setdefault(str(table).lower(), {})[
                str(column).lower()
            ] = f"{ref_table}.{ref_column}".lower()
    except Exception:
        # No referential views to read; degrade to no foreign keys.
        pass

    schema = {}
    for table, columns in columns_by_table.items():
        pk = primary_keys.get(table, ())
        for pk_col in pk:
            if pk_col in columns:
                columns[pk_col] = columns[pk_col]._replace(primary_key=True)
        schema[table] = IntrospectedTable(
            columns=columns,
            primary_key=pk,
            foreign_keys=foreign_keys.get(table, {}),
            indexes=indexes.get(table, {}),
        )
    return schema


# The whole body of the CHECK constraint MariaDB writes for a JSON column.
_JSON_VALID_RE = re.compile(
    r"^\s*json_valid\(\s*`?(\w+)`?\s*\)\s*$",
    re.IGNORECASE,
)


def _mysql_plan() -> SchemaPlan:
    schema = yield from _information_schema_plan(MYSQL_CATALOG)
    yield from _recover_mariadb_json(schema)
    return schema


def _recover_mariadb_json(
    schema: Dict[str, IntrospectedTable],
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
            "WHERE constraint_schema = DATABASE()"
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
        type_params(column.raw_type),
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
