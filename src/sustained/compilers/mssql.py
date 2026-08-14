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
