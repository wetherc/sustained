"""
SQL expression classes.
"""

from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from .types import CaseResult


class Column:
    """
    Represents a column name or a raw SQL expression that should not be quoted.
    """

    def __init__(self, name: str):
        self.name = name

    def __str__(self) -> str:
        return self.name


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
    ):
        """
        Initializes the window function expression.

        Args:
            function_name: The name of the window function (e.g., 'ROW_NUMBER').
            alias: The alias for the resulting column.
            partition_by: A list of columns to partition the window by.
            order_by: A list of columns to order the window by.
        """
        self.function_name = function_name
        self.alias = alias
        self.partition_by = partition_by
        self.order_by = order_by

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
