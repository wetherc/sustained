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

from .builders import JoinClauseBuilder, WhereClauseBuilder

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
        self._with_clauses: List[Tuple[str, str]] = []

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

    def __str__(self) -> str:
        """
        Builds and returns the final SQL query string.

        Returns:
            str: The complete SQL query.
        """
        query_parts = []

        if self._with_clauses:
            cte_strs = [
                f"{alias} AS ({subquery_str})"
                for alias, subquery_str in self._with_clauses
            ]
            with_str = "WITH " + ", ".join(cte_strs)
            query_parts.append(with_str)

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

        query_parts.append(f"SELECT {cols} FROM {full_table_name}")

        if joins_str:
            query_parts.append(joins_str)

        if where_str:
            query_parts.append(where_str)

        return " ".join(query_parts)

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

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )
