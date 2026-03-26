from __future__ import annotations

import re
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
)

from .builders import (
    GroupByClauseBuilder,
    HavingClauseBuilder,
    JoinClauseBuilder,
    WhereClauseBuilder,
)

if TYPE_CHECKING:
    from .model import Model


class QueryBuilder:
    """
    A builder for creating and executing SQL queries in a programmatic way.

    This class is not meant to be instantiated directly. Instead, you should use
    the `query()` class method on a `Model` subclass.
    """

    def __init__(self, model_class: Type["Model"]):
        """
        Initializes the QueryBuilder.

        Args:
            model_class (Type[Model]): The `Model` subclass this query is based on.
        """
        self._model_class = model_class
        self._selected_columns: List[str] = []
        self._join_builder = JoinClauseBuilder(model_class)
        self._where_builder = WhereClauseBuilder(model_class)
        self._group_by_builder = GroupByClauseBuilder(model_class)
        self._having_builder = HavingClauseBuilder(model_class)
        self._with_clauses: List[Tuple[str, str]] = []
        self._offset_value: Optional[int] = None
        self._union_clauses: List[Tuple[str, "QueryBuilder"]] = []
        self._limit_value: Optional[int] = None
        self._top_value: Optional[int] = None

    def select(self, *columns: str) -> "QueryBuilder":
        """
        Specifies the columns to be selected in the query.

        Args:
            *columns (str): A list of column names to select.

        Returns:
            QueryBuilder: The current QueryBuilder instance for chaining.
        """
        self._selected_columns.extend(columns)
        return self

    def with_(self, table_alias: str, subquery: "QueryBuilder") -> "QueryBuilder":
        """
        Adds a Common Table Expression (CTE) to the query.
        NOTE: This method is named `with_` to avoid conflict with the Python `with` keyword.

        Example:
            cte_query = OtherModel.query().select("id", "name")
            main_query = MyModel.query().with_("my_cte", cte_query).select("*")

        Args:
            table_alias (str): The alias for the CTE.
            subquery (QueryBuilder): The query builder instance for the CTE's subquery.

        Returns:
            QueryBuilder: The current QueryBuilder instance for chaining.
        """
        self._with_clauses.append((table_alias, str(subquery)))
        return self

    def groupBy(self, *columns: str) -> "QueryBuilder":
        """
        Specifies the columns to group the query by.

        Args:
            *columns (str): A list of column names to group by.

        Returns:
            QueryBuilder: The current QueryBuilder instance for chaining.
        """
        self._group_by_builder.groupBy(*columns)
        return self

    @staticmethod
    def raw(sql: str) -> str:
        """
        Allows injecting raw SQL fragments into the query.

        Args:
            sql (str): The raw SQL string.

        Returns:
            str: The raw SQL string itself.
        """
        return sql

    def _build_base_select_sql(self) -> str:
        query_parts = []
        cols = ", ".join(self._selected_columns) if self._selected_columns else "*"
        model_cls = self._model_class
        parts = []
        if model_cls.database:
            parts.append(model_cls.database)
        if model_cls.tableSchema:
            parts.append(model_cls.tableSchema)
        if model_cls.tableName:
            parts.append(model_cls.tableName)
        full_table_name = ".".join(parts)

        joins_str = str(self._join_builder)
        where_str = str(self._where_builder)
        group_by_str = str(self._group_by_builder)
        having_str = str(self._having_builder)

        select_parts = ["SELECT"]
        if self._top_value is not None:
            select_parts.append(f"TOP {self._top_value}")

        select_parts.append(cols)
        select_clause = " ".join(select_parts)

        if full_table_name:
            query_parts.append(f"{select_clause} FROM {full_table_name}")
        else:
            query_parts.append(select_clause)

        if joins_str:
            query_parts.append(joins_str)

        if where_str:
            query_parts.append(where_str)

        if group_by_str:
            query_parts.append(group_by_str)

        if having_str:
            query_parts.append(having_str)

        return " ".join(query_parts)

    def __str__(self) -> str:
        """
        Builds and returns the final SQL query string.
        Returns:
            str: The complete SQL query.
        """
        # Hoist all CTEs to the top level.
        all_with_clauses = self._with_clauses[:]
        if self._union_clauses:
            for _, query in self._union_clauses:
                # The subquery's __str__ will be called by the `with_` method,
                # so we need to get the clauses from the builder instance directly.
                all_with_clauses.extend(query._with_clauses)

        query_parts = []
        if all_with_clauses:
            # Use a dictionary to handle potential duplicate CTE aliases,
            # letting the last one win.
            unique_ctes = {alias: subquery for alias, subquery in all_with_clauses}
            cte_strs = [
                f"{alias} AS ({subquery_str})"
                for alias, subquery_str in unique_ctes.items()
            ]
            with_str = "WITH " + ", ".join(cte_strs)
            query_parts.append(with_str)

        # Build the main query part.
        base_select = self._build_base_select_sql()

        if self._union_clauses:
            # If there are unions, wrap the SELECT statements in parentheses.
            query_parts.append(f"({base_select})")
            for union_type, query in self._union_clauses:
                query_parts.append(union_type)
                base_union_select = query._build_base_select_sql()
                query_parts.append(f"({base_union_select})")
        else:
            # Otherwise, just add the base select statement.
            query_parts.append(base_select)

        # Append LIMIT and OFFSET clauses, which apply to the entire query.
        if self._limit_value is not None:
            query_parts.append(f"LIMIT {self._limit_value}")

        if self._offset_value is not None:
            query_parts.append(f"OFFSET {self._offset_value}")

        return " ".join(query_parts)

    def limit(self, value: int) -> "QueryBuilder":
        """
        Specifies the maximum number of rows to return.
        Args:
            value (int): The maximum number of rows.
        Returns:
            QueryBuilder: The current QueryBuilder instance for chaining.
        """
        if not isinstance(value, int):
            raise TypeError("LIMIT value must be an integer.")
        if self._limit_value is not None:
            raise ValueError("LIMIT can only be set once per query.")
        if self._top_value is not None:
            raise ValueError("Cannot use limit() with top().")
        self._limit_value = value
        return self

    def top(self, value: int) -> "QueryBuilder":
        """
        Specifies the top N rows to return (SQL Server-style TOP syntax).
        Args:
            value (int): The number of rows to return.
        Returns:
            QueryBuilder: The current QueryBuilder instance for chaining.
        """
        if not isinstance(value, int):
            raise TypeError("TOP value must be an integer.")
        if self._top_value is not None:
            raise ValueError("TOP can only be set once per query.")
        if self._limit_value is not None:
            raise ValueError("Cannot use top() with limit().")
        self._top_value = value
        return self

    def union(self, *queries: "QueryBuilder", all: bool = False) -> "QueryBuilder":
        """
        Adds one or more UNION clauses to the query.
        Args:
            *queries (QueryBuilder): The subqueries to be unioned.
            all (bool): If True, performs a UNION ALL. Defaults to False.
        Returns:
            QueryBuilder: The current QueryBuilder instance for chaining.
        """
        union_type = "UNION ALL" if all else "UNION"
        for q in queries:
            self._union_clauses.append((union_type, q))
        return self

    def unionAll(self, *queries: "QueryBuilder") -> "QueryBuilder":
        """
        Adds one or more UNION ALL clauses to the query.
        Args:
            *queries (QueryBuilder): The subqueries to be unioned.
        Returns:
            QueryBuilder: The current QueryBuilder instance for chaining.
        """
        return self.union(*queries, all=True)

    def offset(self, value: int) -> "QueryBuilder":
        """
        Specifies the offset for the query.

        Args:
            value (int): The number of rows to skip.

        Returns:
            QueryBuilder: The current QueryBuilder instance for chaining.
        """
        if not isinstance(value, int):
            raise TypeError("Offset value must be an integer.")
        if self._offset_value is not None:
            raise ValueError("Offset can only be set once per query.")
        self._offset_value = value
        return self

    def __getattr__(self, name: str) -> Callable[..., "QueryBuilder"]:
        """
        Dynamically handles method calls for joins and where clauses.

        This allows for methods like `where('id', '=', 1)`, `orWhere(...)`,
        `innerJoinRelated('owner')`, etc., to be called on the query builder.
        """
        # Handle join methods by delegating to JoinClauseBuilder
        join_prefixes = "|".join(
            k for k in self._join_builder._JOIN_METHOD_MAP.keys() if k
        )
        join_match = re.match(
            rf"^({join_prefixes})?(Join)(Related)?$", name, re.IGNORECASE
        )
        if join_match:

            def dynamic_caller(*args: Any, **kwargs: Any) -> "QueryBuilder":
                method_to_call = getattr(self._join_builder, name)
                method_to_call(*args, **kwargs)
                return self

            return dynamic_caller

        # Handle where methods by delegating to WhereClauseBuilder
        where_match = re.match(
            r"^(or|and)?(Where|WhereIn|WhereNotIn)$", name, re.IGNORECASE
        )
        if where_match:

            def dynamic_caller(*args: Any, **kwargs: Any) -> "QueryBuilder":
                method_to_call = getattr(self._where_builder, name)
                method_to_call(*args, **kwargs)
                return self

            return dynamic_caller

        # Handle having methods by delegating to HavingClauseBuilder
        having_match = re.match(
            r"^(or|and)?(Having|HavingIn|HavingNotIn)$", name, re.IGNORECASE
        )
        if having_match:

            def dynamic_caller(*args: Any, **kwargs: Any) -> "QueryBuilder":
                method_to_call = getattr(self._having_builder, name)
                method_to_call(*args, **kwargs)
                return self

            return dynamic_caller

        # Handle group by methods by delegating to GroupByClauseBuilder
        group_by_match = re.match(r"^groupBy$", name, re.IGNORECASE)
        if group_by_match:

            def dynamic_caller(*args: Any, **kwargs: Any) -> "QueryBuilder":
                method_to_call = getattr(self._group_by_builder, name)
                method_to_call(*args, **kwargs)
                return self

            return dynamic_caller

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )
