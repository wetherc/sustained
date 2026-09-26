"""
What a schema read reports: the tables, columns, indexes, and foreign
keys of one snapshot, and the plan type every dialect's read returns.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import (
    Dict,
    Generator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from sustained.types import RowValue


class SchemaRecorder(Protocol):
    """
    What async_introspect_schema() needs from a recorder: one call per
    plan statement, with the rows it returned or the error it raised.
    SchemaRead in sustained.migrations is the implementation the async
    migrator hands in.
    """

    def record(
        self,
        sql: str,
        rows: List[Sequence[RowValue]],
        error: Optional[Exception] = None,
    ) -> None: ...


class IntrospectedColumn(NamedTuple):
    """
    One column as reported by the database.

    `default` is the catalog's report, which is what a diff compares.
    `default_sql` is the same default written as SQL that a DEFAULT
    clause accepts. It is None when `default` is already SQL, which is
    the case everywhere except MySQL: its catalog reports the literal
    raw for 'raw' and the expression uuid() for (uuid()).

    `autoincrement` is True where the catalog reports the column as an
    identity column. Only the MySQL read sets it, from the EXTRA column,
    because only MySQL restates a whole column to change its comment.

    `collation` is the collating sequence the column was declared with,
    or None when it names none. The SQLite read takes it from the stored
    CREATE TABLE statement, so a table rebuild can write it back. The
    MySQL and SQL Server reads take it from COLLATION_NAME, so a
    statement that restates the column can restate it too. MySQL gives
    a restated column the table's collation, and SQL Server the
    database's, unless the statement names one.

    `on_update` is the expression of MySQL's ON UPDATE clause, such as
    CURRENT_TIMESTAMP(3), read from the EXTRA column. MODIFY COLUMN drops
    the clause unless it restates it. It is None everywhere else.

    `name` is the column's name as the catalog spells it. A snapshot
    keys every name in lower case, and Postgres takes a quoted name as
    written, so a statement that names "Email" as "email" names another
    column. It is None where a read does not keep it.
    """

    raw_type: str
    nullable: bool
    primary_key: bool
    default: Optional[str] = None
    enum_name: Optional[str] = None
    enum_values: Tuple[str, ...] = ()
    comment: Optional[str] = None
    default_sql: Optional[str] = None
    autoincrement: bool = False
    collation: Optional[str] = None
    name: Optional[str] = None
    on_update: Optional[str] = None

    def restated_default(self) -> Optional[str]:
        """The default as SQL text for a DEFAULT clause, or None."""
        return self.default if self.default_sql is None else self.default_sql


class IntrospectedIndex(NamedTuple):
    """
    One index as reported by the database.

    `constraint` is True for the index behind a UNIQUE constraint. The
    engine drops that index with the constraint, through DROP
    CONSTRAINT, and refuses DROP INDEX on it. On SQLite it is an
    automatic index, which only a table rebuild removes.

    `name` is the index name as the catalog spells it, or None where a
    read does not keep it.
    """

    columns: Tuple[str, ...]
    unique: bool
    constraint: bool = False
    name: Optional[str] = None


class IntrospectedForeignKey(NamedTuple):
    """
    One foreign key constraint as reported by the database. On engines
    whose catalog does not say where a key points, target_table is '?'
    and target_columns is empty. Actions are None when the engine does
    not report them. `name` is the constraint name as the catalog spells
    it, or None where a read does not keep it.

    `target_schema` is the schema of the target table, as the catalog
    spells it, when that schema is not the one the connection is on. It
    is None for a target in the connection's schema, and wherever a read
    does not report it. A key restored to a target in another schema
    needs the schema, or it points at a table of the same name in the
    connection's schema.
    """

    columns: Tuple[str, ...]
    target_table: str
    target_columns: Tuple[str, ...] = ()
    on_delete: Optional[str] = None
    on_update: Optional[str] = None
    name: Optional[str] = None
    target_schema: Optional[str] = None


# Defaults for tables introspected without keys, indexes, or checks. A
# NamedTuple shares one default object across every instance, so these are
# read-only to keep one table's empty mapping from ever becoming another's.
_NO_FOREIGN_KEYS: Mapping[str, IntrospectedForeignKey] = MappingProxyType({})
_NO_INDEXES: Mapping[str, IntrospectedIndex] = MappingProxyType({})
_NO_CHECKS: Mapping[str, str] = MappingProxyType({})


class IntrospectedTable(NamedTuple):
    """
    One table as reported by the database.

    `unnamed_checks` and `triggers` are read on SQLite only, where a
    table rebuild has to write them back. An unnamed check is the
    expression of a CHECK written without a CONSTRAINT name, at the
    column or at the table level. A trigger is its CREATE TRIGGER
    statement as SQLite stored it.

    `name` is the table name as the catalog spells it, and `check_names`
    maps each lowercased check name to its spelling. Either is empty
    where a read does not keep it.

    `schema` is the schema the table is in, as the catalog spells it,
    when that schema is one the models declare. It is None for a table
    in the schema the connection is on, and for every table on an engine
    that has no schemas. A statement that names a table outside the
    connection's schema needs the schema in front of the name, or it
    names a table that is not there.
    """

    columns: Dict[str, IntrospectedColumn]
    primary_key: Tuple[str, ...] = ()
    foreign_keys: Mapping[str, IntrospectedForeignKey] = _NO_FOREIGN_KEYS
    indexes: Mapping[str, IntrospectedIndex] = _NO_INDEXES
    checks: Mapping[str, str] = _NO_CHECKS
    unnamed_checks: Tuple[str, ...] = ()
    triggers: Tuple[str, ...] = ()
    name: Optional[str] = None
    check_names: Mapping[str, str] = _NO_CHECKS
    schema: Optional[str] = None

    def spelled_column(self, key: str) -> str:
        """A lowercased column key as the catalog spells the column."""
        column = self.columns.get(key)
        return key if column is None or column.name is None else column.name

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
        views: Sequence[str] = (),
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
        # The names of the views in the schema, read on SQLite only. A
        # table rebuild renames its copy into place, and SQLite refuses
        # the rename while a view names a table that is not there.
        self.views: Tuple[str, ...] = tuple(views)

    def copy(self) -> "Snapshot":
        """
        A copy that a rename can rewrite without changing this snapshot.
        A rename replaces tables and edits a table's columns mapping in
        place, so both are copied. Everything else in a table is replaced
        whole, never edited, and is shared.
        """
        return Snapshot(
            {
                key: table._replace(columns=dict(table.columns))
                for key, table in self.items()
            },
            self.enum_types,
            self.enum_types_read,
            self.constraints_read,
            self.checks_read,
            self.comments_read,
            self.views,
        )


# A schema read expressed as a sequence of queries. The plan yields one
# statement at a time and receives its rows back, so the same reading
# code serves a blocking connection and an async adapter. A statement
# that fails is thrown back in, and the plan decides whether to degrade
# or give up.
SchemaPlan = Generator[str, List[Sequence[RowValue]], Snapshot]
