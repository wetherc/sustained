"""
SQL expression classes.
"""

from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple, Union

if TYPE_CHECKING:
    from .builder import QueryBuilder
    from .rendering import RenderContext
    from .types import CaseResult


class Predicate:
    """
    A composable SQL condition. Build predicates from ColumnExpr comparisons
    and combine them with & (AND), | (OR), and ~ (NOT). Pass the result to
    where() or having().
    """

    def __init__(self, render: "Callable[[RenderContext], str]") -> None:
        self._render = render

    def render(self, ctx: "RenderContext") -> str:
        return self._render(ctx)

    def __and__(self, other: "Predicate") -> "Predicate":
        if not isinstance(other, Predicate):
            return NotImplemented
        return Predicate(lambda ctx: f"({self.render(ctx)} AND {other.render(ctx)})")

    def __or__(self, other: "Predicate") -> "Predicate":
        if not isinstance(other, Predicate):
            return NotImplemented
        return Predicate(lambda ctx: f"({self.render(ctx)} OR {other.render(ctx)})")

    def __invert__(self) -> "Predicate":
        return Predicate(lambda ctx: f"NOT ({self.render(ctx)})")

    def __bool__(self) -> bool:
        raise TypeError(
            "A Predicate has no truth value. Combine predicates with & and | "
            "instead of 'and' and 'or'."
        )


class ColumnExpr:
    """
    A typed column reference that builds Predicate objects from Python
    comparison operators.

    Create one with col('users.age') or through a model's column namespace,
    Model.c.age. Comparing against None with == or != renders IS NULL or
    IS NOT NULL.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"ColumnExpr({self.name!r})"

    def __hash__(self) -> int:
        return hash(self.name)

    def _quoted(self, ctx: "RenderContext") -> str:
        return ctx.compiler.quote_column_reference(self.name)

    def _compare(self, operator: str, value: Any) -> Predicate:
        if value is None:
            if operator == "=":
                return self.is_null()
            if operator in ("!=", "<>"):
                return self.not_null()
            raise ValueError(
                f"Cannot compare a column to None with the '{operator}' operator."
            )

        def render(ctx: "RenderContext") -> str:
            if isinstance(value, ColumnExpr):
                return f"{self._quoted(ctx)} {operator} {value._quoted(ctx)}"
            return f"{self._quoted(ctx)} {operator} {ctx.value(value)}"

        return Predicate(render)

    def __eq__(self, value: object) -> Predicate:  # type: ignore[override]
        return self._compare("=", value)

    def __ne__(self, value: object) -> Predicate:  # type: ignore[override]
        return self._compare("!=", value)

    def __gt__(self, value: Any) -> Predicate:
        return self._compare(">", value)

    def __ge__(self, value: Any) -> Predicate:
        return self._compare(">=", value)

    def __lt__(self, value: Any) -> Predicate:
        return self._compare("<", value)

    def __le__(self, value: Any) -> Predicate:
        return self._compare("<=", value)

    def like(self, pattern: str) -> Predicate:
        return Predicate(
            lambda ctx: ctx.compiler.compile_like(
                self._quoted(ctx), ctx.value(pattern), "LIKE"
            )
        )

    def not_like(self, pattern: str) -> Predicate:
        return Predicate(
            lambda ctx: ctx.compiler.compile_like(
                self._quoted(ctx), ctx.value(pattern), "NOT LIKE"
            )
        )

    def ilike(self, pattern: str) -> Predicate:
        return Predicate(
            lambda ctx: ctx.compiler.compile_like(
                self._quoted(ctx), ctx.value(pattern), "ILIKE"
            )
        )

    def in_(self, values: "Union[List[Any], QueryBuilder[Any]]") -> Predicate:
        return self._in("IN", values)

    def not_in(self, values: "Union[List[Any], QueryBuilder[Any]]") -> Predicate:
        return self._in("NOT IN", values)

    def _in(
        self, operator: str, values: "Union[List[Any], QueryBuilder[Any]]"
    ) -> Predicate:
        if isinstance(values, list):
            if not values:
                raise ValueError("IN/NOT IN requires a non-empty list of values.")
            items = list(values)

            def render(ctx: "RenderContext") -> str:
                rendered = ", ".join(ctx.value(v) for v in items)
                return f"{self._quoted(ctx)} {operator} ({rendered})"

            return Predicate(render)

        def render_sub(ctx: "RenderContext") -> str:
            return f"{self._quoted(ctx)} {operator} ({values._render_sql(ctx)})"

        return Predicate(render_sub)

    def between(self, low: Any, high: Any) -> Predicate:
        return Predicate(
            lambda ctx: (
                f"{self._quoted(ctx)} BETWEEN {ctx.value(low)} AND {ctx.value(high)}"
            )
        )

    def not_between(self, low: Any, high: Any) -> Predicate:
        return Predicate(
            lambda ctx: (
                f"{self._quoted(ctx)} NOT BETWEEN "
                f"{ctx.value(low)} AND {ctx.value(high)}"
            )
        )

    def is_null(self) -> Predicate:
        return Predicate(lambda ctx: f"{self._quoted(ctx)} IS NULL")

    def not_null(self) -> Predicate:
        return Predicate(lambda ctx: f"{self._quoted(ctx)} IS NOT NULL")


def col(name: str) -> ColumnExpr:
    """Creates a typed column reference, e.g. col('users.age') > 21."""
    return ColumnExpr(name)


class Column:
    """
    Represents a column name or a raw SQL expression that should not be quoted.
    """

    def __init__(self, name: str):
        self.name = name

    def __str__(self) -> str:
        return self.name


class Literal:
    """
    Wraps a Python value that should render as a SQL literal.

    Bare strings passed to functions are treated as column references, so a
    string literal argument must be wrapped: Func('COALESCE', 'nickname',
    Literal('N/A')).
    """

    def __init__(self, value: Any):
        self.value = value


class Func:
    """
    Represents a generic SQL function call.
    """

    def __init__(self, function_name: str, *args: Any, alias: Optional[str] = None):
        """
        Initializes the function expression.

        Args:
            function_name: The name of the SQL function (e.g., 'COALESCE').
            *args: The arguments to the function.
            alias: An optional alias for the function expression.
        """
        self.function_name = function_name
        self.args = args
        self.alias = alias


class Subquery:
    """
    Represents a subquery in a SELECT clause.
    """

    def __init__(self, query: "QueryBuilder[Any]", alias: str):
        """
        Initializes the subquery expression.

        Args:
            query: The QueryBuilder instance for the subquery.
            alias: The alias for the subquery result.
        """
        self.query = query
        self.alias = alias

    def __str__(self) -> str:
        """
        Renders the subquery expression as a SQL string.
        """
        return f"({self.query}) AS {self.alias}"


class AggregateExpression:
    """
    Represents a SQL aggregate function call, like COUNT() or SUM().
    """

    def __init__(self, function_name: str, column: str, alias: Optional[str] = None):
        """
        Initializes the aggregate expression.

        Args:
            function_name: The name of the aggregate function (e.g., 'COUNT').
            column: The column to aggregate.
            alias: An optional alias for the expression.
        """
        self.function_name = function_name
        self.column = column
        self.alias = alias

    def __str__(self) -> str:
        """
        Renders the aggregate expression as a SQL string.

        Returns:
            The SQL string representation.
        """
        sql = f"{self.function_name}({self.column})"
        if self.alias:
            sql += f" AS {self.alias}"
        return sql


class WindowExpression:
    """
    Represents a SQL window function call, like ROW_NUMBER() OVER (...).
    """

    def __init__(
        self,
        function_name: str,
        alias: str,
        partition_by: Optional[List[str]] = None,
        order_by: Optional[List[str]] = None,
        args: Optional[List[Any]] = None,
        frame: Optional[str] = None,
    ):
        """
        Initializes the window function expression.

        Args:
            function_name: The name of the window function (e.g., 'ROW_NUMBER').
            alias: The alias for the resulting column.
            partition_by: A list of columns to partition the window by.
            order_by: A list of columns to order the window by. Entries may
                carry a direction suffix, e.g. 'created_at DESC'.
            args: Arguments for the window function itself, e.g. the column
                for LAG or SUM. Strings are column references; wrap literal
                values in Literal().
            frame: An optional frame clause, e.g.
                'ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW'.
        """
        self.function_name = function_name
        self.alias = alias
        self.partition_by = partition_by
        self.order_by = order_by
        self.args = args or []
        self.frame = frame

    def __str__(self) -> str:
        """
        Renders the window function as a SQL string.

        Returns:
            The SQL string representation.
        """
        over_clauses = []
        if self.partition_by:
            over_clauses.append(f"PARTITION BY {', '.join(self.partition_by)}")
        if self.order_by:
            over_clauses.append(f"ORDER BY {', '.join(self.order_by)}")

        over_sql = " ".join(over_clauses)
        # Check if over_sql is empty before adding parentheses
        if over_sql:
            return f"{self.function_name}() OVER ({over_sql}) AS {self.alias}"
        else:
            return f"{self.function_name}() OVER () AS {self.alias}"


class CaseExpression:
    """
    Represents a SQL CASE expression.
    """

    def __init__(self, alias: str, else_result: "CaseResult"):
        """
        Initializes the CASE expression.

        Args:
            alias: The alias for the resulting column.
            else_result: The result to return if no WHEN conditions match.
        """
        self.alias = alias
        self.else_result = else_result
        self._whens: List[Tuple[str, "CaseResult"]] = []

    @property
    def whens(self) -> List[Tuple[str, "CaseResult"]]:
        """The accumulated (condition, result) pairs."""
        return list(self._whens)

    def when(self, condition: str, result: "CaseResult") -> "CaseExpression":
        """
        Adds a WHEN/THEN clause to the CASE expression.

        Args:
            condition: The SQL condition for the WHEN clause.
            result: The result to return if the condition is met.

        Returns:
            The CaseExpression instance for chaining.
        """
        self._whens.append((condition, result))
        return self

    def __str__(self) -> str:
        """
        Renders the CASE expression as a SQL string.

        Returns:
            The SQL string representation.
        """
        sql = "CASE"
        for condition, result in self._whens:
            sql += f" WHEN {condition} THEN {self._format_result(result)}"

        sql += f" ELSE {self._format_result(self.else_result)} END AS {self.alias}"
        return sql

    def _format_result(self, result: "CaseResult") -> str:
        """
        Formats a result value for inclusion in the SQL string.
        """
        if isinstance(result, Column):
            return str(result)
        if isinstance(result, str):
            return f"'{result}'"
        return str(result)
