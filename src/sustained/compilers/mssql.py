from typing import Optional

from sustained.exceptions import DialectError

from .base import Compiler


class MssqlCompiler(Compiler):
    def quote_identifier(self, identifier: str) -> str:
        return f"[{identifier}]"

    def quote_fully_qualified_identifier(self, identifier: str) -> str:
        return ".".join(f"[{part}]" for part in identifier.split("."))

    def compile_top(self, value: int) -> str:
        return f"TOP {value}"

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
        "JSON": "NVARCHAR(MAX)",
    }

    def compile_identity(self) -> str:
        return "IDENTITY(1,1)"

    def compile_add_column(self, table_sql: str, column_sql: str) -> str:
        # T-SQL spells it ADD without the COLUMN keyword.
        return f"ALTER TABLE {table_sql} ADD {column_sql}"

    def compile_rename_column(
        self, table_sql: str, old_name: str, new_name: str
    ) -> str:
        # T-SQL renames through sp_rename with an unquoted object path.
        table_path = table_sql.replace("[", "").replace("]", "")
        return f"EXEC sp_rename '{table_path}.{old_name}', '{new_name}', 'COLUMN'"

    def compile_rename_table(self, old_sql: str, new_sql: str) -> str:
        old_path = old_sql.replace("[", "").replace("]", "")
        new_path = new_sql.replace("[", "").replace("]", "")
        return f"EXEC sp_rename '{old_path}', '{new_path}'"

    def compile_drop_index(self, index_name: str, table_sql: str) -> str:
        # T-SQL requires the table in DROP INDEX.
        return f"DROP INDEX {self.quote_identifier(index_name)} ON {table_sql}"

    def supports_alter_column(self) -> bool:
        return True

    def compile_alter_column_type(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        using: "str | None" = None,
    ) -> "list[str]":
        column_sql = self.quote_identifier(column_name)
        return [f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} {type_sql}"]

    def compile_alter_column_nullability(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        nullable: bool,
    ) -> "list[str]":
        # T-SQL restates the full column definition.
        column_sql = self.quote_identifier(column_name)
        null_sql = "NULL" if nullable else "NOT NULL"
        return [
            f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} "
            f"{type_sql} {null_sql}"
        ]

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

    def migration_lock_sql(self, name: str) -> "list[str]":
        # Session-owned and reentrant; released on disconnect. The negative
        # timeout waits until the lock is free.
        return [
            f"EXEC sp_getapplock @Resource = {self.format_value(name)}, "
            "@LockMode = 'Exclusive', @LockOwner = 'Session', @LockTimeout = -1"
        ]

    def migration_unlock_sql(self, name: str) -> "list[str]":
        return [
            f"EXEC sp_releaseapplock @Resource = {self.format_value(name)}, "
            "@LockOwner = 'Session'"
        ]
