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
