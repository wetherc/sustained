import re
from typing import TYPE_CHECKING, Any, Optional, Union

from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Column,
    Func,
    Literal,
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
        Renders a LIKE or ILIKE predicate. ILIKE is a Postgres extension, so
        the base compiler emulates it by lowercasing both sides. Dialects
        with native ILIKE override this.
        """
        if operator == "ILIKE":
            return f"LOWER({column_sql}) LIKE LOWER({pattern_sql})"
        if operator == "NOT ILIKE":
            return f"LOWER({column_sql}) NOT LIKE LOWER({pattern_sql})"
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

    def compile_upsert_statement(
        self,
        table_sql: str,
        column_names: "list[str]",
        row_values_sql: "list[str]",
        conflict_columns: "list[str]",
        action: str,
        update_columns: "list[str]",
    ) -> str:
        """
        Renders an insert with conflict handling. The base form is the
        ON CONFLICT syntax shared by Postgres, SQLite, and DuckDB. Dialects
        with a different upsert statement override this.
        """
        columns_sql = ", ".join(self.quote_identifier(c) for c in column_names)
        sql = (
            f"INSERT INTO {table_sql} ({columns_sql}) "
            f"VALUES {', '.join(row_values_sql)}"
        )
        conflict_sql = ", ".join(self.quote_identifier(c) for c in conflict_columns)
        if action == "ignore":
            return f"{sql} ON CONFLICT ({conflict_sql}) DO NOTHING"
        assignments = ", ".join(
            f"{self.quote_identifier(c)} = EXCLUDED.{self.quote_identifier(c)}"
            for c in update_columns
        )
        return f"{sql} ON CONFLICT ({conflict_sql}) DO UPDATE SET {assignments}"

    def compile_returning(self, columns_sql: str) -> str:
        """
        Renders a RETURNING clause for DML statements. Dialects without
        support raise DialectError.
        """
        return f"RETURNING {columns_sql}"

    def compile_ctas(self, table_sql: str, select_sql: str, temporary: bool) -> str:
        """
        Renders a CREATE TABLE ... AS statement. Dialects with a different
        shape raise DialectError.
        """
        keyword = "CREATE TEMPORARY TABLE" if temporary else "CREATE TABLE"
        return f"{keyword} {table_sql} AS {select_sql}"

    def compile_top(self, value: int) -> str:
        from sustained.exceptions import DialectError

        raise DialectError(
            f"TOP is not supported by the '{self._dialect.name}' dialect. Use limit() instead."
        )

    def compile_limit_offset(
        self,
        limit: Optional[int],
        offset: Optional[int],
        has_order_by: bool = False,
    ) -> str:
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
            order_cols = ", ".join(self._quote_order_entry(c) for c in window.order_by)
            over_clauses.append(f"ORDER BY {order_cols}")
        if window.frame:
            over_clauses.append(window.frame)
        over_sql = " ".join(over_clauses)
        args_sql = ", ".join(self._format_arg(arg) for arg in window.args)
        alias_sql = self.quote_identifier(window.alias)
        return f"{window.function_name}({args_sql}) OVER ({over_sql}) AS {alias_sql}"

    def _quote_order_entry(self, entry: str) -> str:
        """Quotes an ORDER BY entry that may carry an ASC or DESC suffix."""
        parts = entry.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].upper() in ("ASC", "DESC"):
            return f"{self.quote_column_reference(parts[0])} {parts[1].upper()}"
        return self.quote_column_reference(entry)

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
        Formats a function argument for inclusion in the SQL string.

        Strings are treated as column references and quoted per dialect.
        Literal values must be wrapped in Literal(). Numbers, booleans, and
        None render as literals directly.
        """
        if isinstance(arg, Func):
            return self.compile_function(arg)
        if isinstance(arg, Literal):
            return self.format_value(arg.value)
        if isinstance(arg, AggregateExpression):
            return self.compile_aggregate(arg)
        if isinstance(
            arg,
            (
                Column,
                Expression,
                WindowExpression,
                CaseExpression,
                Subquery,
            ),
        ):
            return str(arg)
        if isinstance(arg, str):
            if arg == "*" or _IDENTIFIER_PATH_RE.match(arg):
                return self.quote_column_reference(arg)
            raise ValueError(
                f"Function argument {arg!r} is not a column name. "
                "Wrap literal values in Literal() or raw SQL in Column()."
            )
        if arg is None or isinstance(arg, (bool, int, float)):
            return self.format_value(arg)
        raise TypeError(
            f"Cannot render a function argument of type {type(arg).__name__}."
        )
