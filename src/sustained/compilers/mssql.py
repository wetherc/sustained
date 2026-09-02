import re
from typing import TYPE_CHECKING, Optional, Sequence

from sustained.exceptions import DialectError

from .base import Compiler

# One [bracketed] segment of a quoted path, with ']]' standing for a ']'
# inside the name.
_BRACKETED_SEGMENT_RE = re.compile(r"\[((?:[^\]]|\]\])*)\]")

if TYPE_CHECKING:
    from sustained.schema import ColumnState


class MssqlCompiler(Compiler):
    def quote_identifier(self, identifier: str) -> str:
        # A closing bracket inside the name doubles, so a name can never
        # end the quoted span early.
        return "[{}]".format(identifier.replace("]", "]]"))

    def quote_fully_qualified_identifier(self, identifier: str) -> str:
        return ".".join(self.quote_identifier(part) for part in identifier.split("."))

    def compile_top(self, value: int) -> str:
        return f"TOP {value}"

    def parenthesized_set_members(self) -> bool:
        return True

    def compile_upsert_statement(
        self,
        table_sql: str,
        column_names: "list[str]",
        row_values_sql: "list[str]",
        conflict_columns: "list[str]",
        action: str,
        update_columns: "list[str]",
    ) -> str:
        # T-SQL has no ON CONFLICT; MERGE covers both upsert actions. The
        # trailing semicolon is required by the MERGE grammar.
        columns_sql = ", ".join(self.quote_identifier(c) for c in column_names)
        on_sql = " AND ".join(
            f"target.{self.quote_identifier(c)} = source.{self.quote_identifier(c)}"
            for c in conflict_columns
        )
        sql = (
            f"MERGE INTO {table_sql} AS target "
            f"USING (VALUES {', '.join(row_values_sql)}) AS source ({columns_sql}) "
            f"ON {on_sql}"
        )
        if action == "merge":
            assignments = ", ".join(
                f"target.{self.quote_identifier(c)} = source.{self.quote_identifier(c)}"
                for c in update_columns
            )
            sql += f" WHEN MATCHED THEN UPDATE SET {assignments}"
        insert_values = ", ".join(
            f"source.{self.quote_identifier(c)}" for c in column_names
        )
        sql += (
            f" WHEN NOT MATCHED THEN INSERT ({columns_sql}) "
            f"VALUES ({insert_values});"
        )
        return sql

    def compile_returning(self, columns_sql: str) -> str:
        raise DialectError(
            "MSSQL does not support RETURNING. Use an OUTPUT clause via raw SQL."
        )

    def compile_ctas(self, table_sql: str, select_sql: str, temporary: bool) -> str:
        raise DialectError(
            "MSSQL does not support CREATE TABLE AS. Use SELECT ... INTO via raw SQL."
        )

    _TYPE_MAP = {
        **Compiler._TYPE_MAP,
        "VARCHAR": "NVARCHAR",
        "TEXT": "NVARCHAR(MAX)",
        "BOOLEAN": "BIT",
        "FLOAT": "FLOAT",
        "TIMESTAMP": "DATETIME2",
        "BINARY": "VARBINARY(MAX)",
        "JSON": "NVARCHAR(MAX)",
    }

    def compile_identity(self) -> str:
        return "IDENTITY(1,1)"

    def compile_add_column(self, table_sql: str, column_sql: str) -> str:
        # T-SQL spells it ADD without the COLUMN keyword.
        return f"ALTER TABLE {table_sql} ADD {column_sql}"

    def _unbracketed_segments(self, sql: str) -> "list[str]":
        """
        Splits a bracket-quoted path back into its raw name segments.

        sp_rename takes the object path as a string literal, not as
        bracketed SQL, so the brackets come off and a doubled ']' inside a
        name goes back to one. A path that carries no brackets splits on
        the dot.
        """
        segments = _BRACKETED_SEGMENT_RE.findall(sql)
        if not segments:
            return sql.split(".")
        return [segment.replace("]]", "]") for segment in segments]

    def compile_rename_column(
        self, table_sql: str, old_name: str, new_name: str
    ) -> str:
        # T-SQL renames through sp_rename with an unquoted object path.
        table_path = ".".join(self._unbracketed_segments(table_sql))
        old_path = self.format_value(f"{table_path}.{old_name}")
        return f"EXEC sp_rename {old_path}, {self.format_value(new_name)}, 'COLUMN'"

    def compile_rename_table(self, old_sql: str, new_sql: str) -> str:
        old_path = ".".join(self._unbracketed_segments(old_sql))
        # sp_rename refuses a qualified new name: the table keeps the schema
        # it already has, so only the final segment is passed.
        new_name = self._unbracketed_segments(new_sql)[-1]
        return f"EXEC sp_rename {self.format_value(old_path)}, {self.format_value(new_name)}"

    def compile_drop_index(self, index_name: str, table_sql: str) -> str:
        # T-SQL requires the table in DROP INDEX.
        return f"DROP INDEX {self.quote_identifier(index_name)} ON {table_sql}"

    def compile_create_table(
        self, table_sql: str, body: str, suffix_sql: str, if_missing: bool
    ) -> str:
        # CREATE TABLE takes no IF NOT EXISTS clause on SQL Server, so the
        # statement runs behind a catalog check instead. OBJECT_ID reads the
        # bracketed name as written.
        create = f"CREATE TABLE {table_sql} ({body}){suffix_sql}"
        if not if_missing:
            return create
        return f"IF OBJECT_ID({self.format_value(table_sql)}, 'U') IS NULL {create}"

    def supports_alter_column(self) -> bool:
        return True

    def _alter_column_sql(
        self, table_sql: str, column_name: str, state: "ColumnState"
    ) -> str:
        """
        The ALTER COLUMN statement that restates one column.

        ALTER COLUMN takes the type and the nullability together. Left
        off, the nullability follows the ANSI_NULL_DFLT setting of the
        session, which turns a NOT NULL column nullable without saying
        so. A default lives in its own constraint here and survives the
        statement, and SQL Server keeps no column comments.
        """
        column_sql = self.quote_identifier(column_name)
        null_sql = "NULL" if state.nullable else "NOT NULL"
        return (
            f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} "
            f"{state.type_sql} {null_sql}"
        )

    def compile_alter_column_type(
        self,
        table_sql: str,
        column_name: str,
        column: "ColumnState",
        using: "str | None" = None,
    ) -> "list[str]":
        return [self._alter_column_sql(table_sql, column_name, column)]

    def compile_alter_column_nullability(
        self,
        table_sql: str,
        column_name: str,
        column: "ColumnState",
    ) -> "list[str]":
        return [self._alter_column_sql(table_sql, column_name, column)]

    def compile_with_keyword(self, recursive: bool) -> str:
        # T-SQL uses plain WITH for recursive CTEs.
        return "WITH"

    def compile_explain(self, analyze: bool) -> str:
        raise DialectError(
            "MSSQL has no EXPLAIN statement. Use SET SHOWPLAN_XML via raw SQL."
        )

    def compile_boolean(self, value: bool) -> str:
        # T-SQL has no boolean literals; BIT columns compare against 1 and 0.
        return "1" if value else "0"

    def compile_limit_offset(
        self,
        limit: Optional[int],
        offset: Optional[int],
        has_order_by: bool = False,
    ) -> str:
        if limit is None and offset is None:
            return ""
        # T-SQL only allows OFFSET/FETCH after an ORDER BY clause.
        if not has_order_by:
            raise DialectError(
                "MSSQL requires an ORDER BY clause to use limit() or offset(). "
                "Add orderBy(), or use top() for a simple row cap."
            )
        parts = [f"OFFSET {offset if offset is not None else 0} ROWS"]
        if limit is not None:
            parts.append(f"FETCH NEXT {limit} ROWS ONLY")
        return " ".join(parts)

    def begin_transaction_sql(self) -> Optional[str]:
        # Bare BEGIN opens a statement block in T-SQL, not a transaction.
        return "BEGIN TRANSACTION"

    def rollback_transaction_sql(self) -> Optional[str]:
        return "ROLLBACK TRANSACTION"

    def savepoint_sql(self, name: str) -> Optional[str]:
        # T-SQL spells the ANSI SAVEPOINT statement as SAVE TRANSACTION.
        return f"SAVE TRANSACTION {name}"

    def rollback_savepoint_sql(self, name: str) -> Optional[str]:
        return f"ROLLBACK TRANSACTION {name}"

    def release_savepoint_sql(self, name: str) -> Optional[str]:
        # T-SQL has no RELEASE; savepoints last until the transaction ends.
        return None

    def migration_lock_sql(self, name: str) -> "list[str]":
        # Session-owned and reentrant; released on disconnect. The negative
        # timeout waits until the lock is free. sp_getapplock reports a
        # refused lock in its return status rather than raising, so the
        # batch selects that status for migration_lock_problem() to read.
        return [
            "DECLARE @sustained_lock int; "
            f"EXEC @sustained_lock = sp_getapplock @Resource = "
            f"{self.format_value(name)}, @LockMode = 'Exclusive', "
            "@LockOwner = 'Session', @LockTimeout = -1; "
            "SELECT @sustained_lock"
        ]

    def migration_unlock_sql(self, name: str) -> "list[str]":
        return [
            f"EXEC sp_releaseapplock @Resource = {self.format_value(name)}, "
            "@LockOwner = 'Session'"
        ]

    def migration_lock_problem(
        self, row: "Optional[Sequence[object]]"
    ) -> Optional[str]:
        # sp_getapplock returns 0 or 1 when the lock was granted and a
        # negative status when it was not: -1 timeout, -2 cancelled, -3
        # deadlock victim, -999 a bad parameter.
        status = self.lock_status(row)
        if status is None:
            value = row[0] if row else None
            return f"sp_getapplock returned {value!r} instead of a status"
        if status >= 0:
            return None
        return f"sp_getapplock returned status {status}"
