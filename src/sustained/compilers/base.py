from typing import TYPE_CHECKING, Any, Optional, Union

from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Column,
    Func,
    Subquery,
    WindowExpression,
)
from sustained.types import DbReturnValue, Expression

if TYPE_CHECKING:
    from sustained.dialects import Dialects


class Compiler:
    def __init__(self, dialect: "Dialects") -> None:
        self._dialect = dialect

    def quote_identifier(self, identifier: str) -> str:
        return identifier

    def quote_fully_qualified_identifier(self, identifier: str) -> str:
        return identifier

    def placeholder(self) -> str:
        return "?"

    def format_value(self, value: Union[Expression, DbReturnValue]) -> str:
        if isinstance(value, Expression):
            return str(value)
        elif isinstance(value, str):
            escaped_value = value.replace("'", "''")
            return f"'{escaped_value}'"
        else:
            return str(value)

    def compile_top(self, value: int) -> str:
        return ""

    def compile_limit_offset(self, limit: Optional[int], offset: Optional[int]) -> str:
        parts = []
        if limit is not None:
            parts.append(f"LIMIT {limit}")
        if offset is not None:
            parts.append(f"OFFSET {offset}")
        return " ".join(parts)

    def compile_function(self, func: Func) -> str:
        """
        Renders a Func expression as a SQL string.
        """
        formatted_args = ", ".join(self._format_arg(arg) for arg in func.args)
        sql = f"{func.function_name.upper()}({formatted_args})"
        if func.alias:
            sql += f" AS {self.quote_identifier(func.alias)}"
        return sql

    def _format_arg(self, arg: Any) -> str:
        """
        Formats an argument for inclusion in the SQL string.
        """
        if isinstance(arg, Func):
            return self.compile_function(arg)
        if isinstance(
            arg,
            (
                Column,
                Expression,
                AggregateExpression,
                WindowExpression,
                CaseExpression,
                Subquery,
            ),
        ):
            return str(arg)
        if isinstance(arg, str):
            return f"'{arg}'"
        return str(arg)
