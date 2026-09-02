"""
Reading a live schema, and comparing two reads.

introspect_schema() and async_introspect_schema() report the tables,
columns, primary keys, unique constraints, foreign keys, defaults,
indexes, check constraints, column comments, and enum types a database
currently holds.
Both drive the same generator-based plan, so one dialect's reading code
serves a blocking connection and an async adapter alike.

diff_snapshots() compares two such reads. A rehearsal uses it to check
that the down steps put the schema back where it started.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Callable,
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
from sustained.execution import cursor_scope
from sustained.types import Connection, RowValue

if TYPE_CHECKING:
    from sustained.aio import AsyncAdapter


class IntrospectedColumn(NamedTuple):
    """One column as reported by the database."""

    raw_type: str
    nullable: bool
    primary_key: bool
    default: Optional[str] = None
    enum_name: Optional[str] = None
    enum_values: Tuple[str, ...] = ()
    comment: Optional[str] = None


class IntrospectedIndex(NamedTuple):
    """One index as reported by the database."""

    columns: Tuple[str, ...]
    unique: bool


class IntrospectedForeignKey(NamedTuple):
    """
    One foreign key constraint as reported by the database. On engines
    whose catalog does not say where a key points, target_table is '?'
    and target_columns is empty. Actions are None when the engine does
    not report them.
    """

    columns: Tuple[str, ...]
    target_table: str
    target_columns: Tuple[str, ...] = ()
    on_delete: Optional[str] = None
    on_update: Optional[str] = None


# Defaults for tables introspected without keys, indexes, or checks. A
# NamedTuple shares one default object across every instance, so these are
# read-only to keep one table's empty mapping from ever becoming another's.
_NO_FOREIGN_KEYS: Mapping[str, IntrospectedForeignKey] = MappingProxyType({})
_NO_INDEXES: Mapping[str, IntrospectedIndex] = MappingProxyType({})
_NO_CHECKS: Mapping[str, str] = MappingProxyType({})


class IntrospectedTable(NamedTuple):
    """One table as reported by the database."""

    columns: Dict[str, IntrospectedColumn]
    primary_key: Tuple[str, ...] = ()
    foreign_keys: Mapping[str, IntrospectedForeignKey] = _NO_FOREIGN_KEYS
    indexes: Mapping[str, IntrospectedIndex] = _NO_INDEXES
    checks: Mapping[str, str] = _NO_CHECKS

    @property
    def foreign_key_targets(self) -> Dict[str, str]:
        """
        Each foreign key column mapped to the 'table.column' it points at,
        or '?' when the engine's catalog does not say. This is the mapping
        foreign_keys held before constraints were read by name.
        """
        targets: Dict[str, str] = {}
        for fk in self.foreign_keys.values():
            for position, column in enumerate(fk.columns):
                if fk.target_table == "?":
                    targets[column] = "?"
                elif position < len(fk.target_columns):
                    targets[column] = f"{fk.target_table}.{fk.target_columns[position]}"
                else:
                    targets[column] = fk.target_table
        return targets


class Snapshot(Dict[str, IntrospectedTable]):
    """
    One schema read: tables keyed by lowercased name, plus the standalone
    enum types the database holds. It is a dict, so every caller that
    wants only the tables reads it as one.
    """

    def __init__(
        self,
        tables: Optional[Mapping[str, IntrospectedTable]] = None,
        enum_types: Optional[Mapping[str, Tuple[str, ...]]] = None,
        enum_types_read: bool = False,
        constraints_read: bool = False,
        checks_read: bool = False,
        comments_read: bool = False,
    ) -> None:
        super().__init__(tables or {})
        self.enum_types: Dict[str, Tuple[str, ...]] = dict(enum_types or {})
        # Whether the engine's catalog of standalone enum types was read.
        # Postgres reads pg_enum, so an absent type there really is
        # absent. Engines without such a read leave this False, and a
        # diff must not take an empty mapping as proof of absence.
        self.enum_types_read = enum_types_read
        # Whether foreign key constraints were read by name, and whether
        # check constraints were read at all. A degraded read leaves the
        # flag False, and a diff must not take an empty mapping as proof
        # that a constraint is absent.
        self.constraints_read = constraints_read
        self.checks_read = checks_read
        # Whether column comments were read. SQLite and MSSQL store none,
        # and a degraded read leaves the flag False, so a diff must not
        # take an absent comment as proof the database holds none.
        self.comments_read = comments_read


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
    # Binary columns. MySQL sizes them in the type name the way it sizes
    # text; Postgres calls the type bytea; MSSQL reports varbinary.
    "BYTEA": "BINARY",
    "BLOB": "BINARY",
    "TINYBLOB": "BINARY",
    "MEDIUMBLOB": "BINARY",
    "LONGBLOB": "BINARY",
    "VARBINARY": "BINARY",
    "BINARY VARYING": "BINARY",
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


# The parts of an expression a normalization must not touch: a string
# literal keeps its spaces and its quoting characters.
_SQL_LITERAL_RE = re.compile(r"('(?:[^']|'')*')")
# An identifier the engine quoted on the way in: "col", `col`, [col].
_QUOTED_IDENTIFIER_RE = re.compile(r'"([^"]*)"|`([^`]*)`|\[([^\]]*)\]')
# The spacing an engine puts around an operator, which it rewrites to
# its own taste: MSSQL reports [price]>(0) for price > 0.
_OPERATOR_SPACING_RE = re.compile(r"\s*([<>=!+\-*/%,])\s*")
# Parentheses around one word, which MSSQL writes around every literal.
_LONE_PARENS_RE = re.compile(r"\((\w+)\)")


def _outside_literals(value: str, change: Callable[[str], str]) -> str:
    """`change` applied to every part of `value` outside a string literal."""
    parts = _SQL_LITERAL_RE.split(value)
    return "".join(
        part if index % 2 else change(part) for index, part in enumerate(parts)
    )


def _unquote_identifiers(text: str) -> str:
    """The text with the quoting characters off its identifiers."""
    return _QUOTED_IDENTIFIER_RE.sub(
        lambda match: next(group for group in match.groups() if group is not None),
        text,
    )


def normalize_check(expression: str) -> str:
    """
    Reduces a check expression to a comparable form: whitespace collapsed,
    identifier quoting removed, operator spacing and parentheses around a
    single word taken off, balanced outer parentheses stripped, and
    casefolded. String literals keep their spelling.

    The rewriting is what an engine does to a check on the way in. MySQL
    and MariaDB report `price` > 0 for the expression price > 0, and
    MSSQL reports ([price]>(0)). A model declares the bare expression, so
    without this the two would never compare equal and the difference
    would stand as a note no migration can close. Engines rewrite
    expressions further than this repairs, so two spellings that compare
    equal are the same check, while a mismatch is only a doubt.
    """
    value = re.sub(r"\s+", " ", expression).strip()
    value = _outside_literals(value, _unquote_identifiers)
    value = _outside_literals(value, lambda part: _OPERATOR_SPACING_RE.sub(r"\1", part))
    value = _outside_literals(value, lambda part: _LONE_PARENS_RE.sub(r"\1", part))
    while (
        value.startswith("(")
        and value.endswith(")")
        and _balanced_paren_body(value, 0) == value[1:-1]
    ):
        value = value[1:-1].strip()
    return value.casefold()


# A Postgres ::type cast, with the length or precision it may carry and
# any array brackets, so 'x'::character varying(255) reduces to 'x'.
_CAST_RE = re.compile(
    r"::\s*[a-zA-Z_][a-zA-Z_0-9 ]*(?:\s*\(\s*[\d,\s]*\))?(?:\s*\[\s*\])*"
)

_SEQUENCE_DEFAULT_RE = re.compile(r"^\s*nextval\s*\(", re.IGNORECASE)


def is_sequence_default(raw: Optional[str]) -> bool:
    """
    Whether a reported default is a call to a sequence, which is how
    Postgres spells a serial column's default. A model has no way to
    declare one, so there is nothing to compare it against.
    """
    return raw is not None and bool(_SEQUENCE_DEFAULT_RE.match(str(raw)))


def normalize_default(raw: Optional[str]) -> Optional[str]:
    """
    Reduces a reported column default to a comparable form: strips
    balanced outer parentheses, Postgres ::type casts, quotes, and an
    empty argument list, and uppercases. The argument list is why
    MariaDB's current_timestamp() and MySQL's CURRENT_TIMESTAMP compare
    equal.

    A sequence call reduces to None. It is what Postgres reports for a
    serial column, and no model declaration can ever equal it.

    Parentheses come off only when they balance, so an expression such
    as (1)+(2) keeps its shape. The cast comes off before the quotes,
    since the cast may carry a length that the quote strip would leave
    behind.
    """
    if raw is None:
        return None
    if is_sequence_default(raw):
        return None
    value = str(raw).strip()
    while (
        value.startswith("(")
        and value.endswith(")")
        and _balanced_paren_body(value, 0) == value[1:-1]
    ):
        value = value[1:-1].strip()
    value = _CAST_RE.sub("", value)
    value = value.strip("'\"")
    value = re.sub(r"\(\s*\)$", "", value.strip())
    return value.upper()


# A schema read expressed as a sequence of queries. The plan yields one
# statement at a time and receives its rows back, so the same reading
# code serves a blocking connection and an async adapter. A statement
# that fails is thrown back in, and the plan decides whether to degrade
# or give up.
SchemaPlan = Generator[str, List[Sequence[RowValue]], Snapshot]


def _finished(stop: StopIteration) -> Snapshot:
    """The schema a finished plan carried on its StopIteration."""
    return cast(Snapshot, stop.value)


# The savepoint a guarded read takes before each statement.
_READ_SAVEPOINT = "sustained_read"


def _dooms_transaction(dialect: Dialects) -> bool:
    """
    Whether one failed statement stops every later statement in the same
    transaction. A plan tries a view and falls back when it is not there,
    so on such an engine the failure of one query would carry away the
    whole read. Postgres works this way. Each statement runs inside a
    savepoint there, and a failure rolls back to it.
    """
    return dialect == Dialects.POSTGRES


def introspect_schema(
    connection: Connection,
    dialect: Dialects = Dialects.DEFAULT,
    schemas: Sequence[str] = (),
) -> Snapshot:
    """
    Reads tables, columns, primary keys, unique constraints, foreign keys,
    defaults, indexes, check constraints, and column comments from the
    database. Comments come from pg_description on Postgres,
    information_schema.columns on MySQL, Presto, and Athena, and
    duckdb_columns() on DuckDB; SQLite and MSSQL store none, so their
    snapshots leave comments_read False. The
    default dialect reads SQLite's PRAGMA tables and the table SQL in
    sqlite_master. Postgres reads information_schema together with
    pg_index, pg_constraint, pg_enum, and the check view, so every index is
    visible, varchar lengths and numeric precision survive, enum columns
    report their type's name and values, foreign keys resolve with their
    names and actions, and the snapshot carries the database's enum
    types. MySQL and MariaDB add information_schema.statistics, MSSQL
    adds sys.indexes, and DuckDB adds duckdb_indexes(), so plain indexes
    are visible on those engines too. Other dialects read plain
    information_schema and degrade to column-only data when constraint
    views are unavailable. Names are keyed lowercase.
    """
    plan = _schema_plan(dialect, tuple(schemas))
    guarded = [_dooms_transaction(dialect)]
    # An open transaction reads on its own cursor: a rehearsal introspects
    # a schema its uncommitted statements just changed, and on DuckDB a
    # fresh cursor is a separate session that cannot see it. One cursor
    # serves the whole plan and is given back when the read finishes; a
    # plan is a dozen or more queries, and the last one's rows would sit
    # unread on it otherwise.
    with cursor_scope(connection) as cursor:

        def run(sql: str) -> List[Sequence[RowValue]]:
            if not guarded[0]:
                cursor.execute(sql)
                return list(cursor.fetchall())
            try:
                cursor.execute(f"SAVEPOINT {_READ_SAVEPOINT}")
            except Exception:
                # A connection in autocommit has no transaction to take a
                # savepoint in, and no transaction to lose either.
                guarded[0] = False
                cursor.execute(sql)
                return list(cursor.fetchall())
            try:
                cursor.execute(sql)
                rows = list(cursor.fetchall())
            except Exception:
                try:
                    cursor.execute(f"ROLLBACK TO SAVEPOINT {_READ_SAVEPOINT}")
                except Exception:
                    pass
                raise
            cursor.execute(f"RELEASE SAVEPOINT {_READ_SAVEPOINT}")
            return rows

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
    adapter: "AsyncAdapter",
    dialect: Dialects = Dialects.DEFAULT,
    schemas: Sequence[str] = (),
) -> Snapshot:
    """
    Reads the schema through an async adapter, returning what
    introspect_schema() returns. Both run the same reading code, so a
    dialect behaves the same on either path.
    """
    plan = _schema_plan(dialect, tuple(schemas))
    guarded = _dooms_transaction(dialect)

    async def run(sql: str) -> List[Sequence[RowValue]]:
        if not guarded:
            _, rows = await adapter.fetch(sql, ())
            return list(rows)
        try:
            await adapter.execute(f"SAVEPOINT {_READ_SAVEPOINT}", ())
        except Exception:
            # No transaction to take a savepoint in, and none to lose.
            return list((await adapter.fetch(sql, ()))[1])
        try:
            _, rows = await adapter.fetch(sql, ())
        except Exception:
            try:
                await adapter.execute(f"ROLLBACK TO SAVEPOINT {_READ_SAVEPOINT}", ())
            except Exception:
                pass
            raise
        await adapter.execute(f"RELEASE SAVEPOINT {_READ_SAVEPOINT}", ())
        return list(rows)

    sql = next(plan)
    while True:
        try:
            rows = await run(sql)
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


def _schema_plan(dialect: Dialects, schemas: Tuple[str, ...] = ()) -> SchemaPlan:
    if dialect == Dialects.DEFAULT:
        return _sqlite_plan()
    if dialect == Dialects.MYSQL:
        return _mysql_plan(schemas)
    if dialect == Dialects.POSTGRES:
        return _postgres_plan(schemas)
    if dialect == Dialects.MSSQL:
        return _mssql_plan(schemas)
    if dialect == Dialects.DUCKDB:
        return _duckdb_plan(schemas)
    if dialect == Dialects.ATHENA:
        return _information_schema_plan(ATHENA_CATALOG, schemas)
    return _information_schema_plan(PRESTO_CATALOG, schemas)


def _schema_literal(name: str) -> str:
    """One schema name as a SQL string literal."""
    return "'{}'".format(name.replace("'", "''"))


def _schema_predicate(
    column: str, current_sql: Optional[str], schemas: Tuple[str, ...]
) -> Optional[str]:
    """
    The WHERE fragment that holds `column` to the schemas a read covers:
    the connection's own schema when the engine can name it, plus every
    schema the models declare. None when the engine has no expression for
    the current schema and the caller named none, which leaves the read
    unscoped.

    The declared schemas make their own IN list and the current schema
    is compared beside it. An engine expression inlined into the IN list
    would take every declared schema down with it when the expression
    returns NULL, which Postgres does when the first search_path entry
    names a schema that does not exist.
    """
    parts: List[str] = []
    if schemas:
        literals: List[str] = []
        for name in schemas:
            literal = _schema_literal(name)
            if literal not in literals:
                literals.append(literal)
        parts.append("{} IN ({})".format(column, ", ".join(literals)))
    if current_sql is not None:
        parts.append(f"{column} = {current_sql}")
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return "({})".format(" OR ".join(parts))


def _scoped_filter(column: str, current_sql: str, schemas: Tuple[str, ...]) -> str:
    """
    _schema_predicate() for an engine that can name the schema the
    connection is on, where the predicate is never empty.
    """
    predicate = _schema_predicate(column, current_sql, schemas)
    assert predicate is not None
    return predicate


def _strip_identifier(name: str) -> str:
    """An identifier with its quoting characters removed, lowercased."""
    return name.strip().strip('"`[]').lower()


# A named constraint in a CREATE TABLE statement. Checks are read by
# name; a CHECK written without a CONSTRAINT name stays unread. Foreign
# keys match their pragma rows by column list.
_SQLITE_CHECK_RE = re.compile(
    r"CONSTRAINT\s+[\"`\[]?(\w+)[\"`\]]?\s+CHECK\s*\(",
    re.IGNORECASE,
)
_SQLITE_FK_NAME_RE = re.compile(
    r"CONSTRAINT\s+[\"`\[]?(\w+)[\"`\]]?\s+FOREIGN\s+KEY\s*\(([^)]*)\)",
    re.IGNORECASE,
)


def _balanced_paren_body(text: str, start: int) -> Optional[str]:
    """
    The text between the parenthesis at `start` and its matching close,
    or None when the parentheses do not balance. Quoted strings are
    skipped, so a ')' inside a literal does not end the expression.
    """
    depth = 0
    in_string = False
    for position in range(start, len(text)):
        char = text[position]
        if in_string:
            if char == "'":
                in_string = False
            continue
        if char == "'":
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : position]
    return None


def _sqlite_table_checks(create_sql: str) -> Dict[str, str]:
    """
    The named check constraints in a CREATE TABLE statement. SQLite has
    no catalog view for checks, so they are read back out of the stored
    CREATE TABLE SQL. A CHECK with no CONSTRAINT name stays unread.
    """
    checks: Dict[str, str] = {}
    for match in _SQLITE_CHECK_RE.finditer(create_sql):
        body = _balanced_paren_body(create_sql, match.end() - 1)
        if body is not None:
            checks[match.group(1).lower()] = body.strip()
    return checks


def _sqlite_fk_names(create_sql: str) -> Dict[Tuple[str, ...], str]:
    """Named FOREIGN KEY clauses, keyed by their column tuple."""
    names: Dict[Tuple[str, ...], str] = {}
    for match in _SQLITE_FK_NAME_RE.finditer(create_sql):
        columns = tuple(
            _strip_identifier(part)
            for part in match.group(2).split(",")
            if part.strip()
        )
        names[columns] = match.group(1).lower()
    return names


def _sqlite_quote(name: str) -> str:
    """
    Quotes a name for a SQLite PRAGMA.

    A table or index name can legally hold a space or a double quote, and a
    PRAGMA takes the name as SQL, not as a parameter. The name is wrapped in
    double quotes with any inner quote doubled.
    """
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _sqlite_plan() -> SchemaPlan:
    rows = yield (
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    )
    tables = [(row[0], row[1]) for row in rows]
    schema = Snapshot(constraints_read=True, checks_read=True)
    for table, create_sql in tables:
        columns: Dict[str, IntrospectedColumn] = {}
        primary_key: List[str] = []
        for _, name, raw_type, notnull, default, pk in (
            yield f"PRAGMA table_info({_sqlite_quote(table)})"
        ):
            columns[name.lower()] = IntrospectedColumn(
                raw_type=raw_type or "",
                nullable=not notnull,
                primary_key=bool(pk),
                default=default,
            )
            if pk:
                primary_key.append(name.lower())

        fk_rows = sorted(
            (yield f"PRAGMA foreign_key_list({_sqlite_quote(table)})"),
            key=lambda row: (row[0], row[1]),
        )
        rows_by_key: Dict[int, List[Sequence[RowValue]]] = {}
        for row in fk_rows:
            rows_by_key.setdefault(int(cast(int, row[0])), []).append(row)

        declared_fk_names = _sqlite_fk_names(create_sql or "")
        foreign_keys: Dict[str, IntrospectedForeignKey] = {}
        for fk_id, key_rows in rows_by_key.items():
            first = key_rows[0]
            key_columns = tuple(str(r[3]).lower() for r in key_rows)
            name = declared_fk_names.get(key_columns, f"fk_{table.lower()}_{fk_id}")
            foreign_keys[name] = IntrospectedForeignKey(
                columns=key_columns,
                target_table=str(first[2]).lower(),
                target_columns=tuple(
                    str(r[4]).lower() for r in key_rows if r[4] is not None
                ),
                on_delete=None if first[6] is None else str(first[6]),
                on_update=None if first[5] is None else str(first[5]),
            )

        indexes: Dict[str, IntrospectedIndex] = {}
        index_rows = yield f"PRAGMA index_list({_sqlite_quote(table)})"
        for row in index_rows:
            index_name, unique, origin = row[1], bool(row[2]), row[3]
            if origin == "pk":
                continue
            info = yield f"PRAGMA index_info({_sqlite_quote(index_name)})"
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
            checks=_sqlite_table_checks(create_sql or ""),
        )
    return schema


# System schemas excluded from information_schema introspection.
_SYSTEM_SCHEMAS = (
    "'information_schema'",
    "'pg_catalog'",
    "'sys'",
    "'INFORMATION_SCHEMA'",
)


def _is_generated_not_null_check(name: str, expression: str) -> bool:
    """
    Whether a check row is the constraint an engine writes for a NOT NULL
    column. Postgres and DuckDB both report one, named after the column
    with a _not_null suffix. It belongs to the column's own nullable
    flag, not to the table's checks, and a model never declares it.
    """
    return name.endswith("_not_null") and "IS NOT NULL" in expression.upper()


class Catalog(NamedTuple):
    """
    How one engine's information_schema differs from the shared read.

    Attributes:
        schema_filter: The WHERE fragment that picks the schemas to
            read on an engine with no current-schema expression. It takes
            the column reference as {column}.
        type_column: The column holding the type spelling to compare on.
        current_schema_sql: The expression that names the schema the
            connection is on, or None on engines with no such expression.
            A read is scoped to it, plus every schema the models declare.
            An engine that leaves it None keeps schema_filter whatever
            the models declare, so its read stays as wide as it was.
        comment_column: The information_schema.columns column holding the
            column comment, or None on engines that store none there.
        reads_checks: Whether the engine fills
            information_schema.check_constraints. Presto and Athena have
            no CHECK constraints at all, so their snapshots must leave
            checks_read False rather than report an empty mapping.
    """

    schema_filter: str
    type_column: str
    current_schema_sql: Optional[str] = None
    comment_column: Optional[str] = None
    reads_checks: bool = True


ANSI_CATALOG = Catalog(
    schema_filter="{column} NOT IN (" + ", ".join(_SYSTEM_SCHEMAS) + ")",
    type_column="data_type",
)

MYSQL_CATALOG = ANSI_CATALOG._replace(
    # A MySQL schema is a database, and every other database on the server
    # belongs to someone else. DATABASE() is the one the connection is on.
    # Without it here, a model that declares tableSchema would take the
    # connection's own database out of the read.
    current_schema_sql="DATABASE()",
    # data_type reports 'varchar' and keeps the length in a column of its
    # own. column_type reports 'varchar(120)', which is what the compiler
    # emits, so a column never drifts against its own DDL.
    type_column="column_type",
    comment_column="column_comment",
)

# Presto and Trino put the comment straight on information_schema.columns.
# Their tables are files on object storage and hold no CHECK constraints.
PRESTO_CATALOG = ANSI_CATALOG._replace(comment_column="comment", reads_checks=False)

# Athena reads only the connection's schema. Its catalog spans every Glue
# database in the account, so an unscoped read is slow and fails outright
# when any other database holds a table with broken metadata.
ATHENA_CATALOG = PRESTO_CATALOG._replace(current_schema_sql="current_schema")

# MSSQL keys everything on the bare table name, so two schemas holding a
# table with one name would merge. The read covers the connection's own
# schema, plus every schema the models declare.
MSSQL_CATALOG = ANSI_CATALOG._replace(current_schema_sql="SCHEMA_NAME()")

# DuckDB keys on the bare table name the same way, and its own catalog
# functions carry a schema_name column to filter on.
DUCKDB_CATALOG = ANSI_CATALOG._replace(current_schema_sql="current_schema()")


def _catalog_filter(catalog: Catalog, column: str, schemas: Tuple[str, ...]) -> str:
    """
    The WHERE fragment one catalog's read puts on a schema column.

    An engine that can name the schema the connection is on reads that
    schema plus every schema the models declare. An engine that cannot
    keeps its own filter: Presto and Trino read every schema but the
    system ones, and a declared schema must not narrow that read to less
    than it covered before.
    """
    if catalog.current_schema_sql is None:
        return catalog.schema_filter.format(column=column)
    return _scoped_filter(column, catalog.current_schema_sql, schemas)


def _information_schema_plan(
    catalog: Catalog = ANSI_CATALOG, schemas: Tuple[str, ...] = ()
) -> SchemaPlan:
    column_filter = _catalog_filter(catalog, "c.table_schema", schemas)
    constraint_filter = _catalog_filter(catalog, "tc.table_schema", schemas)
    # Two schemas in one read can each hold a constraint with one name.
    # The join below matches schema names to keep them apart, and the
    # plain-join fallback cannot, so it only runs on a read that covers
    # one schema.
    single_schema = catalog.current_schema_sql is not None and not schemas

    columns_by_table: Dict[str, Dict[str, IntrospectedColumn]] = {}

    def columns_query(with_comment: bool) -> str:
        # The join to information_schema.tables keeps views out. A view's
        # columns would read as a table the models do not declare, so one
        # view in the database is enough to make every plan report drift,
        # and allow_drops would emit a DROP TABLE the engine refuses.
        comment = f", c.{catalog.comment_column}" if with_comment else ""
        return (
            f"SELECT c.table_name, c.column_name, c.{catalog.type_column}, "
            f"c.is_nullable, c.column_default{comment} "
            "FROM information_schema.columns c "
            "JOIN information_schema.tables t "
            "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            f"WHERE {column_filter} AND t.table_type = 'BASE TABLE' "
            "ORDER BY c.table_name, c.ordinal_position"
        )

    # The comment travels with the column instead of in a second full read
    # of information_schema.columns. Engines that advertise a comment
    # column but do not have one fall back to the plain read, which is
    # what the second query's own fallback used to do.
    comments_read = catalog.comment_column is not None
    if comments_read:
        try:
            column_rows = yield columns_query(True)
        except Exception:
            comments_read = False
            column_rows = yield columns_query(False)
    else:
        column_rows = yield columns_query(False)

    for row in column_rows:
        table, name, data_type, is_nullable, default = row[:5]
        # MySQL reports an uncommented column as '', not NULL.
        raw_comment = row[5] if len(row) > 5 else None
        comment = str(raw_comment) if raw_comment not in (None, "") else None
        columns_by_table.setdefault(str(table).lower(), {})[str(name).lower()] = (
            IntrospectedColumn(
                raw_type=str(data_type) if data_type else "",
                nullable=str(is_nullable).upper() == "YES",
                primary_key=False,
                default=default,
                comment=comment,
            )
        )

    primary_keys: Dict[str, List[str]] = {}
    unique_indexes: Dict[str, Dict[str, IntrospectedIndex]] = {}
    foreign_keys: Dict[str, Dict[str, IntrospectedForeignKey]] = {}
    constraints_read = False
    # A constraint name is only unique within its schema, so the join
    # matches schemas as well as names. Without that, two schemas each
    # holding a constraint with one name cross-multiply into a garbled
    # column list. An engine whose key_column_usage has no table_schema
    # column takes the plain join instead.
    joins = ["AND tc.table_schema = kcu.table_schema "]
    if single_schema:
        joins.append("")
    for schema_join in joins:
        try:
            constraint_rows = yield (
                "SELECT tc.table_name, tc.constraint_type, tc.constraint_name, "
                "kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "ON tc.constraint_name = kcu.constraint_name "
                "AND tc.table_name = kcu.table_name "
                f"{schema_join}"
                f"WHERE {constraint_filter} "
                "ORDER BY kcu.ordinal_position"
            )
        except Exception:
            # No constraint views, or no table_schema on this one.
            continue
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
                # The referenced table is engine-specific to resolve;
                # presence is enough for constraint notes.
                foreign_keys.setdefault(table, {})[cname] = IntrospectedForeignKey(
                    columns=tuple(cols), target_table="?"
                )
        constraints_read = True
        break

    checks: Dict[str, Dict[str, str]] = {}
    checks_read = False
    if catalog.reads_checks:
        try:
            check_rows = yield (
                "SELECT tc.table_name, tc.constraint_name, cc.check_clause "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.check_constraints cc "
                "ON cc.constraint_schema = tc.constraint_schema "
                "AND cc.constraint_name = tc.constraint_name "
                "WHERE tc.constraint_type = 'CHECK' "
                f"AND {constraint_filter}"
            )
            for table, cname, clause in check_rows:
                name = str(cname).lower()
                expression = str(clause)
                if _is_generated_not_null_check(name, expression):
                    continue
                checks.setdefault(str(table).lower(), {})[name] = expression
            checks_read = True
        except Exception:
            # An engine too old for the check view; degrade to no checks.
            pass

    schema = Snapshot(
        constraints_read=constraints_read,
        checks_read=checks_read,
        comments_read=comments_read,
    )
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
            checks=checks.get(table, {}),
        )
    return schema


def _merge_plain_indexes(
    schema: Snapshot, plain: Dict[str, Dict[str, IntrospectedIndex]]
) -> None:
    """Adds engine-read indexes to the tables the shared read produced."""
    for table, indexes in plain.items():
        existing = schema.get(table)
        if existing is None:
            continue
        merged = dict(existing.indexes)
        merged.update(indexes)
        schema[table] = existing._replace(indexes=merged)


def _mssql_plan(schemas: Tuple[str, ...] = ()) -> SchemaPlan:
    """
    information_schema plus sys.indexes. The shared read sees unique
    constraints only; plain indexes and CREATE UNIQUE INDEX indexes live
    in sys.indexes. Without them, a model's declared index reads as
    missing on every plan, and the second run fails creating it again.
    """
    schema = yield from _information_schema_plan(MSSQL_CATALOG, schemas)
    index_filter = _catalog_filter(MSSQL_CATALOG, "SCHEMA_NAME(t.schema_id)", schemas)
    try:
        index_rows = yield (
            "SELECT t.name, i.name, i.is_unique, c.name "
            "FROM sys.indexes i "
            "JOIN sys.tables t ON t.object_id = i.object_id "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id "
            "AND ic.index_id = i.index_id "
            "JOIN sys.columns c ON c.object_id = ic.object_id "
            "AND c.column_id = ic.column_id "
            "WHERE i.is_primary_key = 0 AND i.is_unique_constraint = 0 "
            "AND i.name IS NOT NULL AND ic.is_included_column = 0 "
            f"AND {index_filter} "
            "ORDER BY t.name, i.name, ic.key_ordinal"
        )
        parts: Dict[Tuple[str, str, bool], List[str]] = {}
        for table, name, is_unique, column in index_rows:
            key = (str(table).lower(), str(name).lower(), bool(is_unique))
            parts.setdefault(key, []).append(str(column).lower())
        plain: Dict[str, Dict[str, IntrospectedIndex]] = {}
        for (table, name, unique), columns in parts.items():
            plain.setdefault(table, {})[name] = IntrospectedIndex(
                tuple(columns), unique
            )
        _merge_plain_indexes(schema, plain)
    except Exception:
        # No sys views to read; keep the constraint-derived indexes.
        pass
    return schema


_DUCKDB_IDENTIFIER_RE = re.compile(r"^\w+$")


def _duckdb_index_columns(expressions: str) -> Optional[Tuple[str, ...]]:
    """
    The column list in duckdb_indexes()'s expressions field, spelled
    '[a, b]'. An expression index has parts that are not bare column
    names; it cannot be compared against a model's column list, so it
    reads as None and stays out of the schema.
    """
    parts = [
        part.strip() for part in expressions.strip("[]").split(",") if part.strip()
    ]
    if not parts or any(not _DUCKDB_IDENTIFIER_RE.match(part) for part in parts):
        return None
    return tuple(part.lower() for part in parts)


def _duckdb_plan(schemas: Tuple[str, ...] = ()) -> SchemaPlan:
    """
    information_schema plus duckdb_indexes(). DuckDB's shared read sees
    unique constraints only, so a model's declared index would read as
    missing on every plan, and the second run would fail creating it
    again.
    """
    schema = yield from _information_schema_plan(DUCKDB_CATALOG, schemas)
    schema_filter = _catalog_filter(DUCKDB_CATALOG, "schema_name", schemas)
    try:
        index_rows = yield (
            "SELECT table_name, index_name, is_unique, expressions "
            f"FROM duckdb_indexes() WHERE NOT is_primary "
            f"AND {schema_filter}"
        )
        plain: Dict[str, Dict[str, IntrospectedIndex]] = {}
        for table, name, is_unique, expressions in index_rows:
            columns = _duckdb_index_columns(str(expressions or ""))
            if columns is None:
                continue
            plain.setdefault(str(table).lower(), {})[str(name).lower()] = (
                IntrospectedIndex(columns, bool(is_unique))
            )
        _merge_plain_indexes(schema, plain)
    except Exception:
        # No duckdb_indexes() to read; keep the constraint-derived indexes.
        pass
    try:
        comment_rows = yield (
            "SELECT table_name, column_name, comment FROM duckdb_columns() "
            f"WHERE comment IS NOT NULL AND {schema_filter}"
        )
        for table, name, comment in comment_rows:
            existing = schema.get(str(table).lower())
            if existing is None:
                continue
            key = str(name).lower()
            column = existing.columns.get(key)
            if column is not None:
                existing.columns[key] = column._replace(comment=str(comment))
        schema.comments_read = True
    except Exception:
        # A DuckDB from before COMMENT ON; degrade to no comments.
        pass
    try:
        type_rows = yield (
            "SELECT type_name, labels FROM duckdb_types() "
            "WHERE labels IS NOT NULL AND NOT internal "
            f"AND {schema_filter}"
        )
        for name, labels in type_rows:
            if not labels:
                continue
            schema.enum_types[str(name).lower()] = tuple(
                str(label) for label in cast(Sequence[RowValue], labels)
            )
        schema.enum_types_read = True
    except Exception:
        # A DuckDB from before duckdb_types(); the diff falls back to
        # inferring a type's presence from the columns that use it.
        pass
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


# How pg_constraint spells a referential action, mapped to the words
# the information_schema views and the model declarations use.
_PG_FK_ACTIONS = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def _pg_fk_action(code: Optional[RowValue]) -> Optional[str]:
    """The action a pg_constraint action character stands for."""
    if code is None:
        return None
    return _PG_FK_ACTIONS.get(str(code), str(code).upper())


def _postgres_plan(schemas: Tuple[str, ...] = ()) -> SchemaPlan:
    # Everything below keys on the bare table name, so an unscoped read
    # merges app.users into public.users and the diff never converges.
    # The read covers the schema the connection is on, plus every schema
    # the models declare.
    # current_schema() returns NULL when the first search_path entry
    # names a schema that does not exist, so it is compared beside the
    # declared schemas instead of standing in an IN list with them.
    table_filter = _scoped_filter("c.table_schema", "current_schema()", schemas)
    constraint_filter = _scoped_filter("tc.table_schema", "current_schema()", schemas)
    namespace_filter = _scoped_filter("n.nspname", "current_schema()", schemas)
    columns_by_table: Dict[str, Dict[str, IntrospectedColumn]] = {}
    column_rows = yield (
        "SELECT c.table_name, c.column_name, c.data_type, c.udt_name, "
        "c.character_maximum_length, c.numeric_precision, c.numeric_scale, "
        "c.is_nullable, c.column_default "
        "FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        f"WHERE {table_filter} "
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
            f"AND {namespace_filter} "
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

    foreign_keys: Dict[str, Dict[str, IntrospectedForeignKey]] = {}
    constraints_read = False
    try:
        # pg_constraint is read instead of the referential_constraints
        # view because a constraint name is unique per table, not per
        # schema. The view joins on the name alone, so two tables in one
        # schema with a same-named key cross-multiply into garbled
        # column lists. conrelid tells the two apart for free.
        fk_rows = yield (
            "SELECT src.relname, con.conname, sa.attname, tgt.relname, "
            "ta.attname, con.confdeltype, con.confupdtype "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class src ON src.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = src.relnamespace "
            "JOIN pg_catalog.pg_class tgt ON tgt.oid = con.confrelid "
            "CROSS JOIN LATERAL unnest(con.conkey, con.confkey) "
            "WITH ORDINALITY AS k(attnum, refattnum, ord) "
            "JOIN pg_catalog.pg_attribute sa "
            "ON sa.attrelid = con.conrelid AND sa.attnum = k.attnum "
            "JOIN pg_catalog.pg_attribute ta "
            "ON ta.attrelid = con.confrelid AND ta.attnum = k.refattnum "
            "WHERE con.contype = 'f' "
            f"AND {namespace_filter} "
            "ORDER BY src.relname, con.conname, k.ord"
        )
        fk_parts: Dict[Tuple[str, str], List[Sequence[RowValue]]] = {}
        for row in fk_rows:
            part_key = (str(row[0]).lower(), str(row[1]).lower())
            fk_parts.setdefault(part_key, []).append(row)
        for (table, cname), rows in fk_parts.items():
            first = rows[0]
            foreign_keys.setdefault(table, {})[cname] = IntrospectedForeignKey(
                columns=tuple(str(r[2]).lower() for r in rows),
                target_table=str(first[3]).lower(),
                target_columns=tuple(str(r[4]).lower() for r in rows),
                on_delete=_pg_fk_action(first[5]),
                on_update=_pg_fk_action(first[6]),
            )
        constraints_read = True
    except Exception:
        # No pg_constraint to read; degrade to no foreign keys.
        pass

    checks: Dict[str, Dict[str, str]] = {}
    checks_read = False
    try:
        check_rows = yield (
            "SELECT tc.table_name, tc.constraint_name, cc.check_clause "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.check_constraints cc "
            "ON cc.constraint_schema = tc.constraint_schema "
            "AND cc.constraint_name = tc.constraint_name "
            "WHERE tc.constraint_type = 'CHECK' "
            f"AND {constraint_filter}"
        )
        for table, cname, clause in check_rows:
            name = str(cname).lower()
            expression = str(clause)
            if _is_generated_not_null_check(name, expression):
                continue
            checks.setdefault(str(table).lower(), {})[name] = expression
        checks_read = True
    except Exception:
        # No check views to read; degrade to no checks.
        pass

    comments: Dict[str, Dict[str, str]] = {}
    comments_read = False
    try:
        comment_rows = yield (
            "SELECT c.relname, a.attname, d.description "
            "FROM pg_catalog.pg_description d "
            "JOIN pg_catalog.pg_class c ON c.oid = d.objoid "
            "JOIN pg_catalog.pg_attribute a "
            "ON a.attrelid = d.objoid AND a.attnum = d.objsubid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE d.objsubid > 0 "
            f"AND {namespace_filter}"
        )
        for table, name, description in comment_rows:
            comments.setdefault(str(table).lower(), {})[str(name).lower()] = str(
                description
            )
        comments_read = True
    except Exception:
        # No pg_description to read; degrade to no comments.
        pass

    enum_types: Dict[str, Tuple[str, ...]] = {}
    enum_types_read = False
    try:
        enum_rows = yield (
            "SELECT t.typname, e.enumlabel "
            "FROM pg_catalog.pg_type t "
            "JOIN pg_catalog.pg_enum e ON e.enumtypid = t.oid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace "
            f"WHERE {namespace_filter} "
            "ORDER BY t.typname, e.enumsortorder"
        )
        values_by_type: Dict[str, List[str]] = {}
        for typname, label in enum_rows:
            values_by_type.setdefault(str(typname).lower(), []).append(str(label))
        enum_types = {name: tuple(vals) for name, vals in values_by_type.items()}
        enum_types_read = True
    except Exception:
        # No pg_enum to read; degrade to no enum types.
        pass

    schema = Snapshot(
        enum_types=enum_types,
        enum_types_read=enum_types_read,
        constraints_read=constraints_read,
        checks_read=checks_read,
        comments_read=comments_read,
    )
    for table, columns in columns_by_table.items():
        pk = primary_keys.get(table, ())
        for pk_col in pk:
            if pk_col in columns:
                columns[pk_col] = columns[pk_col]._replace(primary_key=True)
        for name, column in columns.items():
            values = enum_types.get(column.raw_type.lower())
            if values is not None:
                columns[name] = column._replace(
                    enum_name=column.raw_type.lower(), enum_values=values
                )
        for name, comment in comments.get(table, {}).items():
            if name in columns:
                columns[name] = columns[name]._replace(comment=comment)
        schema[table] = IntrospectedTable(
            columns=columns,
            primary_key=pk,
            foreign_keys=foreign_keys.get(table, {}),
            indexes=indexes.get(table, {}),
            checks=checks.get(table, {}),
        )
    return schema


# The whole body of the CHECK constraint MariaDB writes for a JSON column.
_JSON_VALID_RE = re.compile(
    r"^\s*json_valid\(\s*`?(\w+)`?\s*\)\s*$",
    re.IGNORECASE,
)


_MYSQL_ENUM_RE = re.compile(r"^\s*enum\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_MYSQL_ENUM_VALUE_RE = re.compile(r"'((?:[^']|'')*)'")


def parse_inline_enum(raw_type: str) -> Tuple[str, ...]:
    """
    The values of a MySQL enum('a','b') column type, empty when the type
    is not an enum. A quote inside a value arrives doubled and is put
    back to one.
    """
    match = _MYSQL_ENUM_RE.match(raw_type)
    if match is None:
        return ()
    return tuple(
        value.replace("''", "'")
        for value in _MYSQL_ENUM_VALUE_RE.findall(match.group(1))
    )


def _mysql_plan(schemas: Tuple[str, ...] = ()) -> SchemaPlan:
    # Every query in the plan covers the same schemas. A read that took
    # its columns from one schema and its indexes from another would
    # attach an index to a table of the same name in the wrong schema.
    current = MYSQL_CATALOG.current_schema_sql
    assert current is not None
    table_filter = _scoped_filter("table_schema", current, schemas)
    constraint_filter = _scoped_filter("constraint_schema", current, schemas)
    schema = yield from _information_schema_plan(MYSQL_CATALOG, schemas)
    yield from _recover_mariadb_json(schema, constraint_filter)
    try:
        index_rows = yield (
            "SELECT table_name, index_name, non_unique, column_name "
            "FROM information_schema.statistics "
            f"WHERE {table_filter} "
            "ORDER BY table_name, index_name, seq_in_index"
        )
        parts: Dict[Tuple[str, str, bool], List[Optional[str]]] = {}
        for table, name, non_unique, column in index_rows:
            if str(name).upper() == "PRIMARY":
                continue
            key = (str(table).lower(), str(name).lower(), not int(str(non_unique)))
            parts.setdefault(key, []).append(
                None if column is None else str(column).lower()
            )
        plain: Dict[str, Dict[str, IntrospectedIndex]] = {}
        for (table, name, unique), columns in parts.items():
            if any(column is None for column in columns):
                # A functional index part has no column name; it cannot
                # be compared against a model's column list.
                continue
            plain.setdefault(table, {})[name] = IntrospectedIndex(
                tuple(cast(str, column) for column in columns), unique
            )
        _merge_plain_indexes(schema, plain)
    except Exception:
        # No statistics view; keep the constraint-derived indexes.
        pass
    for table in schema.values():
        for name, column in table.columns.items():
            values = parse_inline_enum(column.raw_type)
            if values:
                # A MySQL enum lives inline on its column, so there is no
                # standalone type name to carry.
                table.columns[name] = column._replace(enum_values=values)
    return schema


def _recover_mariadb_json(
    schema: Dict[str, IntrospectedTable],
    constraint_filter: str,
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
            f"WHERE {constraint_filter}"
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
        # MariaDB writes this constraint itself for every JSON column.
        # It belongs to the column's type, so it is not a check the
        # model failed to declare.
        table_key = str(table).lower()
        remaining = {
            check: expression
            for check, expression in introspected.checks.items()
            if check != name
        }
        schema[table_key] = introspected._replace(checks=remaining)


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

    Tables and columns are compared. Indexes, constraints, defaults, and
    comments are not: their reported spellings differ enough between
    engines that a difference here would report noise as a failure.
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
