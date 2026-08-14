from typing import Optional

from .base import Compiler


class PrestoCompiler(Compiler):
    def quote_identifier(self, identifier: str) -> str:
        return f'"{identifier}"'

    def compile_upsert_statement(
        self,
        table_sql: str,
        column_names: "list[str]",
        row_values_sql: "list[str]",
        conflict_columns: "list[str]",
        action: str,
        update_columns: "list[str]",
    ) -> str:
        from sustained.exceptions import DialectError

        raise DialectError("Presto does not support upserts.")

    def compile_identity(self) -> str:
        from sustained.exceptions import DialectError

        raise DialectError("Presto has no identity columns.")

    def compile_returning(self, columns_sql: str) -> str:
        from sustained.exceptions import DialectError

        raise DialectError("Presto does not support RETURNING clauses.")

    def compile_limit_offset(
        self,
        limit: Optional[int],
        offset: Optional[int],
        has_order_by: bool = False,
    ) -> str:
        # Presto and Trino require OFFSET before LIMIT.
        parts = []
        if offset is not None:
            parts.append(f"OFFSET {offset}")
        if limit is not None:
            parts.append(f"LIMIT {limit}")
        return " ".join(parts)
