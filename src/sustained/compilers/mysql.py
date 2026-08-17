from typing import TYPE_CHECKING, Optional

from sustained.exceptions import DialectError
from sustained.types import SqlValue

from .base import Compiler

if TYPE_CHECKING:
    from sustained.schema import ColumnDef

# MySQL has no way to say "every row from here on". The manual's own
# recipe for an offset without a limit is to ask for the largest row
# count the server accepts.
_ALL_ROWS = 18446744073709551615

# Types that MySQL stores off the row, which it will not index whole and
# will not take a literal DEFAULT for.
_OFF_ROW_TYPES = ("TEXT", "JSON")


class MysqlCompiler(Compiler):
    """
    Compiler for MySQL and MariaDB.

    Identifiers quote with backticks and placeholders are `%s`, matching
    PyMySQL, mysqlclient, and mysql-connector. The type map renders each
    logical type in the spelling `information_schema` reports back, so a
    column never drifts against the DDL that created it.

    MariaDB is served by this same dialect. The two diverge in what they
    report about an existing schema, which the introspector handles, and
    in RETURNING, which MariaDB has and MySQL does not; a query builder
    that emitted it for one would produce SQL the other rejects, so both
    raise here and MariaDB's RETURNING stays reachable through raw SQL.
    """

    def quote_identifier(self, identifier: str) -> str:
        return f"`{identifier}`"

    def placeholder(self) -> str:
        # The %s style used by PyMySQL, mysqlclient, and mysql-connector.
        return "%s"

    def format_value(self, value: SqlValue) -> str:
        if isinstance(value, str):
            # MySQL reads a backslash inside a string literal as an escape
            # character, which no other dialect here does. Left alone,
            # 'C:\path' would reach the server as 'C:path'.
            escaped = value.replace("\\", "\\\\").replace("'", "''")
            return f"'{escaped}'"
        return super().format_value(value)

    _TYPE_MAP = {
        **Compiler._TYPE_MAP,
        "INTEGER": "INT",
        # MySQL's BOOLEAN is a synonym its catalog never repeats back:
        # information_schema reports the underlying tinyint(1).
        "BOOLEAN": "TINYINT(1)",
        "FLOAT": "DOUBLE",
        "NUMERIC": "DECIMAL",
        # TIMESTAMP on MySQL is a four-byte column that stops in 2038 and
        # carries time zone conversion. DATETIME is the plain wall clock
        # the Timestamp() type describes.
        "TIMESTAMP": "DATETIME",
    }

    def compile_identity(self) -> str:
        return "AUTO_INCREMENT"

    def validate_column_def(self, column: "ColumnDef") -> None:
        if column.type_name not in _OFF_ROW_TYPES:
            return
        spelling = self._TYPE_MAP[column.type_name]
        if column.unique or column.primary_key:
            raise DialectError(
                f"MySQL cannot put a unique key on a whole {spelling} column, "
                "because it needs a prefix length. Use String(n) with a "
                "length that fits, or declare the prefix index through a "
                "hand-written migration."
            )
        if column.default is not None:
            raise DialectError(
                f"MySQL takes no literal DEFAULT on a {spelling} column. "
                "Fill the value in the application, or use a backfill on "
                "the column so the migration sets it."
            )

    def compile_upsert_statement(
        self,
        table_sql: str,
        column_names: "list[str]",
        row_values_sql: "list[str]",
        conflict_columns: "list[str]",
        action: str,
        update_columns: "list[str]",
    ) -> str:
        # MySQL has no ON CONFLICT. ON DUPLICATE KEY UPDATE fires on any
        # unique key the row collides with, not only the named columns,
        # which is the one place this statement is wider here than
        # elsewhere.
        columns_sql = ", ".join(self.quote_identifier(c) for c in column_names)
        sql = (
            f"INSERT INTO {table_sql} ({columns_sql}) "
            f"VALUES {', '.join(row_values_sql)}"
        )
        if action == "ignore":
            # The grammar needs an assignment, so the row assigns a column
            # to itself and changes nothing. INSERT IGNORE would do the
            # same in fewer words and also swallow truncation and foreign
            # key errors, which this statement should not.
            keep = self.quote_identifier((conflict_columns or column_names)[0])
            return f"{sql} ON DUPLICATE KEY UPDATE {keep} = {keep}"
        assignments = ", ".join(
            f"{self.quote_identifier(c)} = VALUES({self.quote_identifier(c)})"
            for c in update_columns
        )
        return f"{sql} ON DUPLICATE KEY UPDATE {assignments}"

    def compile_returning(self, columns_sql: str) -> str:
        raise DialectError(
            "MySQL does not support RETURNING. Read the row back with a "
            "second query, or use LAST_INSERT_ID() through raw SQL."
        )

    def compile_limit_offset(
        self,
        limit: Optional[int],
        offset: Optional[int],
        has_order_by: bool = False,
    ) -> str:
        if limit is None and offset is None:
            return ""
        if offset is not None and limit is None:
            return f"LIMIT {_ALL_ROWS} OFFSET {offset}"
        parts = [f"LIMIT {limit}"]
        if offset is not None:
            parts.append(f"OFFSET {offset}")
        return " ".join(parts)

    def compile_locking(self, skip_locked: bool, nowait: bool) -> str:
        clause = "FOR UPDATE"
        if skip_locked:
            clause += " SKIP LOCKED"
        elif nowait:
            clause += " NOWAIT"
        return clause

    def compile_drop_index(self, index_name: str, table_sql: str) -> str:
        # An index name belongs to its table in MySQL, so DROP INDEX names
        # both.
        return f"DROP INDEX {self.quote_identifier(index_name)} ON {table_sql}"

    def supports_alter_column(self) -> bool:
        return True

    def compile_alter_column_type(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        using: Optional[str] = None,
    ) -> "list[str]":
        column_sql = self.quote_identifier(column_name)
        return [f"ALTER TABLE {table_sql} MODIFY COLUMN {column_sql} {type_sql}"]

    def compile_alter_column_nullability(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        nullable: bool,
    ) -> "list[str]":
        # MODIFY restates the whole definition, so the type comes back with
        # the nullability or the column silently becomes nullable.
        column_sql = self.quote_identifier(column_name)
        null_sql = "NULL" if nullable else "NOT NULL"
        return [
            f"ALTER TABLE {table_sql} MODIFY COLUMN {column_sql} "
            f"{type_sql} {null_sql}"
        ]

    def supports_transactional_ddl(self) -> bool:
        # Rows roll back; schema changes do not. Every DDL statement
        # commits as it runs, and a run that fails halfway leaves the
        # statements before it applied.
        return False

    def migration_lock_sql(self, name: str) -> "list[str]":
        # Session-scoped and reentrant, released on disconnect. A negative
        # timeout waits until the lock is free.
        return [f"SELECT GET_LOCK({self.format_value(name)}, -1)"]

    def migration_unlock_sql(self, name: str) -> "list[str]":
        return [f"SELECT RELEASE_LOCK({self.format_value(name)})"]
