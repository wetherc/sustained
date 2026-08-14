import re
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


# A plain identifier path such as "users", "users.id", or "db.dbo.users.id".
_IDENTIFIER_PATH_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$"
)

# Comparison operators accepted by the conditional clause builders. Anything
# else must be expressed with QueryBuilder.raw() so that intent is explicit.
_VALID_OPERATORS = frozenset(
    {
        "=",
        "!=",
        "<>",
        "<",
        "<=",
        ">",
        ">=",
        "LIKE",
        "NOT LIKE",
        "ILIKE",
        "NOT ILIKE",
        "IS",
        "IS NOT",
    }
)


class Compiler:
    def __init__(self, dialect: "Dialects") -> None:
        self._dialect = dialect

    def quote_identifier(self, identifier: str) -> str:
        return identifier

    def quote_fully_qualified_identifier(self, identifier: str) -> str:
        return ".".join(self.quote_identifier(part) for part in identifier.split("."))

    def quote_column_reference(self, column: Union[str, Expression]) -> str:
        """
        Quotes a column reference for use inside a clause.

        Plain identifier paths are quoted per dialect. The star selector and
        anything more complex, such as a function call in a HAVING clause, is
        passed through unchanged. Expression objects are raw SQL.
        """
        if isinstance(column, Expression):
            return str(column)
        if not isinstance(column, str):
            raise TypeError(
                f"Column reference must be a string or Expression, got {type(column).__name__}."
            )
        if column == "*":
            return column
        if _IDENTIFIER_PATH_RE.match(column):
            return self.quote_fully_qualified_identifier(column)
        return column

    def validate_operator(self, operator: str) -> str:
        """
        Normalizes and validates a comparison operator.

        Raises:
            ValueError: If the operator is not a recognized SQL comparison
                operator. Raw predicates should use QueryBuilder.raw().
        """
        if not isinstance(operator, str):
            raise TypeError("Operator must be a string.")
        normalized = " ".join(operator.strip().upper().split())
        if normalized not in _VALID_OPERATORS:
            raise ValueError(
                f"Unsupported SQL operator: {operator!r}. "
                "Use QueryBuilder.raw() for raw SQL predicates."
            )
        return normalized

    def compile_like(self, column_sql: str, pattern_sql: str, operator: str) -> str:
        """
        Renders a LIKE or ILIKE predicate. Dialects without native ILIKE
        override this to emulate case-insensitive matching.
        """
        return f"{column_sql} {operator} {pattern_sql}"

    def placeholder(self) -> str:
        return "?"

    def format_value(self, value: Union[Expression, DbReturnValue, None]) -> str:
        if isinstance(value, Expression):
            return str(value)
        if value is None:
            return "NULL"
        # bool must be checked before int because bool subclasses int.
        if isinstance(value, bool):
            return self.compile_boolean(value)
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            escaped_value = value.replace("'", "''")
            return f"'{escaped_value}'"
        raise TypeError(
            f"Cannot render a value of type {type(value).__name__} as a SQL literal."
        )

    def compile_boolean(self, value: bool) -> str:
        return "TRUE" if value else "FALSE"

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

    def compile_aggregate(self, agg: AggregateExpression) -> str:
        """
        Renders an aggregate expression with dialect quoting for the column
        and the alias.
        """
        column = self.quote_column_reference(agg.column)
        sql = f"{agg.function_name}({column})"
        if agg.alias:
            sql += f" AS {self.quote_identifier(agg.alias)}"
        return sql

    def compile_window(self, window: WindowExpression) -> str:
        """
        Renders a window expression with dialect quoting for partition and
        order columns and the alias.
        """
        over_clauses = []
        if window.partition_by:
            partition_cols = ", ".join(
                self.quote_column_reference(c) for c in window.partition_by
            )
            over_clauses.append(f"PARTITION BY {partition_cols}")
        if window.order_by:
            order_cols = ", ".join(
                self.quote_column_reference(c) for c in window.order_by
            )
            over_clauses.append(f"ORDER BY {order_cols}")
        over_sql = " ".join(over_clauses)
        alias_sql = self.quote_identifier(window.alias)
        return f"{window.function_name}() OVER ({over_sql}) AS {alias_sql}"

    def compile_case(self, case: CaseExpression) -> str:
        """
        Renders a CASE expression. Results go through the dialect's value
        formatting, so booleans and NULL render correctly per dialect.
        """
        sql = "CASE"
        for condition, result in case.whens:
            sql += f" WHEN {condition} THEN {self._format_case_result(result)}"
        sql += f" ELSE {self._format_case_result(case.else_result)}"
        sql += f" END AS {self.quote_identifier(case.alias)}"
        return sql

    def _format_case_result(self, result: Any) -> str:
        if isinstance(result, Column):
            return str(result)
        return self.format_value(result)

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
