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

from sustained.builders import (
    GroupByClauseBuilder,
    HavingClauseBuilder,
    JoinClauseBuilder,
    OrderByClauseBuilder,
    SelectClauseBuilder,
    WhereClauseBuilder,
)
from sustained.dialects import Dialects
from sustained.exceptions import DialectError
from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Column,
    Func,
    WindowExpression,
)
from sustained.functions import FunctionRegistry
from sustained.rendering import RenderContext
from sustained.types import CaseResult, DbReturnValue, Expression, Selectable

if TYPE_CHECKING:
    from sustained.model import Model


def _validate_row_count(value: int, keyword: str) -> None:
    """Rejects values that are not non-negative integers.

    bool is checked explicitly because it is a subclass of int and would
    otherwise render as the words True or False in the SQL output.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{keyword} value must be an integer.")
    if value < 0:
        raise ValueError(f"{keyword} value must not be negative.")


class QueryBuilder:
    """
    A builder for creating and executing SQL queries in a programmatic way.

    This class is not meant to be instantiated directly. Instead, you should use
    the `query()` class method on a `Model` subclass.
    """

    def __init__(self, model_class: Type["Model"], dialect: Optional[Dialects] = None):
        """
        Initializes the QueryBuilder.

        Args:
            model_class (Type[Model]): The `Model` subclass this query is based on.
            dialect (Optional[Dialects]): The SQL dialect to use. Defaults to Dialects.DEFAULT.
        """
        self._model_class = model_class
        self._dialect = dialect if dialect else Dialects.DEFAULT
        self._compiler = Dialects.get_compiler(self._dialect)
        self._select_clause_builder = SelectClauseBuilder(compiler=self._compiler)
        self._join_builder = JoinClauseBuilder(model_class, compiler=self._compiler)
        self._where_builder = WhereClauseBuilder(model_class, compiler=self._compiler)
        self._group_by_builder = GroupByClauseBuilder(
            model_class, compiler=self._compiler
        )
        self._having_builder = HavingClauseBuilder(model_class, compiler=self._compiler)
        self._order_by_builder = OrderByClauseBuilder(
            model_class, compiler=self._compiler
        )
        self._with_clauses: List[Tuple[str, "QueryBuilder"]] = []
        self._offset_value: Optional[int] = None
        self._union_clauses: List[Tuple[str, "QueryBuilder"]] = []
        self._limit_value: Optional[int] = None
        self._top_value: Optional[int] = None
        self._from_source: Optional[Union[str, Tuple["QueryBuilder", str]]] = None
        self._distinct = False

    def distinct(self) -> "QueryBuilder":
        """
        Adds a DISTINCT keyword to the SELECT statement.
        """
        self._distinct = True
        return self

    def _validate_function(self, function_name: str) -> None:
        """
        Checks if a function is supported by the current dialect.

        If the function is not in the registry, it is allowed to pass through
        without validation.

        Args:
            function_name: The name of the function.

        Raises:
            DialectError: If the function is registered but not supported by the dialect.
        """
        try:
            metadata = FunctionRegistry.get_metadata(function_name)
            if self._dialect not in metadata.supported_dialects:
                raise DialectError(
                    f"Function '{function_name.upper()}' is not supported by the '{self._dialect.name}' dialect."
                )
        except KeyError:
            # Function is not registered, allow it to pass through.
            pass

    def select(self, *columns: Selectable) -> "QueryBuilder":
        """
        Specifies the columns to be selected in the query.

        Can accept strings for column names or expression objects for more
        complex selections like aggregates or window functions.

        Args:
            *columns: A list of columns or expression objects to select.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        self._select_clause_builder.select(*columns)
        return self

    def count(self, column: str = "*", alias: Optional[str] = None) -> "QueryBuilder":
        """
        Adds a COUNT() aggregate to the select clause.

        Args:
            column: The column to count. Defaults to '*'.
            alias: An optional alias for the count column.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        self._validate_function("COUNT")
        agg = AggregateExpression("COUNT", column, alias)
        self._select_clause_builder.select(agg)
        return self

    def sum(self, column: str, alias: Optional[str] = None) -> "QueryBuilder":
        """
        Adds a SUM() aggregate to the select clause.

        Args:
            column: The column to sum.
            alias: An optional alias for the sum column.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        self._validate_function("SUM")
        agg = AggregateExpression("SUM", column, alias)
        self._select_clause_builder.select(agg)
        return self

    def avg(self, column: str, alias: Optional[str] = None) -> "QueryBuilder":
        """
        Adds an AVG() aggregate to the select clause.

        Args:
            column: The column to average.
            alias: An optional alias for the average column.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        self._validate_function("AVG")
        agg = AggregateExpression("AVG", column, alias)
        self._select_clause_builder.select(agg)
        return self

    def min(self, column: str, alias: Optional[str] = None) -> "QueryBuilder":
        """
        Adds a MIN() aggregate to the select clause.

        Args:
            column: The column to find the minimum of.
            alias: An optional alias for the min column.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        self._validate_function("MIN")
        agg = AggregateExpression("MIN", column, alias)
        self._select_clause_builder.select(agg)
        return self

    def max(self, column: str, alias: Optional[str] = None) -> "QueryBuilder":
        """
        Adds a MAX() aggregate to the select clause.

        Args:
            column: The column to find the maximum of.
            alias: An optional alias for the max column.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        self._validate_function("MAX")
        agg = AggregateExpression("MAX", column, alias)
        self._select_clause_builder.select(agg)
        return self

    def select_func(
        self, function_name: str, *args: Any, alias: Optional[str] = None
    ) -> "QueryBuilder":
        """
        Adds a generic function call to the select clause.

        Args:
            function_name: The name of the SQL function.
            *args: The arguments for the function.
            alias: An optional alias for the function expression.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        self._validate_function(function_name)
        func = Func(function_name, *args, alias=alias)
        self._select_clause_builder.select(func)
        return self

    def select_window(
        self,
        function_name: str,
        alias: str,
        partition_by: Optional[List[str]] = None,
        order_by: Optional[List[str]] = None,
    ) -> "QueryBuilder":
        """
        Adds a window function to the select clause.

        Args:
            function_name: The name of the window function (e.g., 'ROW_NUMBER').
            alias: The alias for the resulting column.
            partition_by: A list of columns to partition by.
            order_by: A list of columns to order by.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        window = WindowExpression(function_name, alias, partition_by, order_by)
        self._select_clause_builder.select(window)
        return self

    def select_case(
        self,
        alias: str,
        else_result: "CaseResult",
        when_clauses: List[Tuple[str, "CaseResult"]],
    ) -> "QueryBuilder":
        """
        Adds a CASE expression to the select clause.

        Args:
            alias: The alias for the resulting column.
            else_result: The result for the ELSE clause.
            when_clauses: A list of (condition, result) tuples for the WHEN clauses.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        case_expr = CaseExpression(alias, else_result)
        for condition, result in when_clauses:
            case_expr.when(condition, result)
        self._select_clause_builder.select(case_expr)
        return self

    def from_(
        self, table: Union["QueryBuilder", str], alias: Optional[str] = None
    ) -> "QueryBuilder":
        """
        Specifies a table or subquery for the FROM clause.

        Subqueries are rendered when the outer query is rendered, so any
        CTEs they carry are hoisted to the top-level WITH clause.
        """
        if isinstance(table, QueryBuilder):
            if not alias:
                raise ValueError("Subqueries in FROM clause must have an alias.")
            self._from_source = (table, alias)
        elif isinstance(table, str):
            quoted = self._compiler.quote_column_reference(table)
            if alias:
                quoted += f" AS {self._compiler.quote_identifier(alias)}"
            self._from_source = quoted
        else:
            raise TypeError(
                "`from_` method expects a QueryBuilder instance or a raw string."
            )
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
        if not isinstance(subquery, QueryBuilder):
            raise TypeError("CTE subquery must be a QueryBuilder instance.")
        self._with_clauses.append((table_alias, subquery))
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
    def raw(sql: str) -> Expression:
        """
        Allows injecting raw SQL fragments into the query.
        Args:
            sql (str): The raw SQL string.
        Returns:
            Expression: An Expression object that will not be quoted.
        """
        return Expression(sql)

    def _build_base_select_sql(self, ctx: RenderContext) -> str:
        query_parts = []
        cols = str(self._select_clause_builder)

        full_table_name: str
        if isinstance(self._from_source, tuple):
            sub_query, sub_alias = self._from_source
            rendered_sub = sub_query._render_sql(ctx, include_ctes=False)
            quoted_alias = self._compiler.quote_identifier(sub_alias)
            full_table_name = f"({rendered_sub}) AS {quoted_alias}"
        elif isinstance(self._from_source, str):
            full_table_name = self._from_source
        else:
            model_cls = self._model_class
            parts = []
            if model_cls.database:
                parts.append(self._compiler.quote_identifier(model_cls.database))
            if model_cls.tableSchema:
                parts.append(self._compiler.quote_identifier(model_cls.tableSchema))
            if model_cls.tableName:
                parts.append(self._compiler.quote_identifier(model_cls.tableName))
            full_table_name = ".".join(parts)

        joins_str = str(self._join_builder)
        where_str = self._where_builder.render(ctx)
        group_by_str = str(self._group_by_builder)
        having_str = self._having_builder.render(ctx)

        select_parts = ["SELECT"]
        if self._distinct:
            select_parts.append("DISTINCT")

        compiled_top = ""
        if self._top_value is not None:
            compiled_top = self._compiler.compile_top(self._top_value)

        if compiled_top:
            select_parts.append(compiled_top)

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

    def _collect_ctes(self) -> List[Tuple[str, "QueryBuilder"]]:
        """
        Gathers CTEs from this query, its FROM subquery chain, its own CTE
        subqueries, and its union members, in dependency order. All of them
        are rendered in a single top-level WITH clause because WITH cannot
        appear inside a parenthesized subquery in every dialect.
        """
        ctes: List[Tuple[str, "QueryBuilder"]] = []
        if isinstance(self._from_source, tuple):
            ctes.extend(self._from_source[0]._collect_ctes())
        for alias, subquery in self._with_clauses:
            ctes.extend(subquery._collect_ctes())
            ctes.append((alias, subquery))
        for _, query in self._union_clauses:
            ctes.extend(query._collect_ctes())
        return ctes

    def _render_sql(self, ctx: RenderContext, include_ctes: bool = True) -> str:
        """Renders the full statement with the given context."""
        query_parts = []

        if include_ctes:
            collected = self._collect_ctes()
            if collected:
                unique_ctes: Dict[str, "QueryBuilder"] = {}
                for alias, subquery in collected:
                    existing = unique_ctes.get(alias)
                    if existing is not None:
                        if existing is subquery or str(existing) == str(subquery):
                            continue
                        raise ValueError(
                            f"Duplicate CTE alias '{alias}' refers to different subqueries."
                        )
                    unique_ctes[alias] = subquery
                cte_strs = [
                    f"{alias} AS ({subquery._render_sql(ctx, include_ctes=False)})"
                    for alias, subquery in unique_ctes.items()
                ]
                query_parts.append("WITH " + ", ".join(cte_strs))

        # Build the main query part.
        base_select = self._build_base_select_sql(ctx)

        if self._union_clauses:
            # Wrap each SELECT in parentheses. Members render their own
            # ORDER BY and LIMIT inside the parentheses, so per-member
            # clauses are honored rather than silently dropped.
            query_parts.append(f"({base_select})")
            for union_type, query in self._union_clauses:
                query_parts.append(union_type)
                rendered_member = query._render_sql(ctx, include_ctes=False)
                query_parts.append(f"({rendered_member})")
        else:
            query_parts.append(base_select)

        # Append ORDER BY, LIMIT, and OFFSET clauses, which apply to the entire query.
        order_by_str = str(self._order_by_builder)
        if order_by_str:
            query_parts.append(order_by_str)

        limit_offset_str = self._compiler.compile_limit_offset(
            self._limit_value, self._offset_value, has_order_by=bool(order_by_str)
        )
        if limit_offset_str:
            query_parts.append(limit_offset_str)

        return " ".join(query_parts)

    def __str__(self) -> str:
        """
        Builds and returns the final SQL query string with all values
        inlined as SQL literals. Intended for debugging and logging; use
        to_sql() when executing against a database.

        Returns:
            str: The complete SQL query.
        """
        return self._render_sql(RenderContext(self._compiler))

    def to_sql(self) -> Tuple[str, Tuple[DbReturnValue, ...]]:
        """
        Builds the query as a parameterized statement.

        Values supplied to WHERE, HAVING, IN, BETWEEN, and LIKE clauses are
        replaced with the dialect's placeholder and returned separately, in
        the order they appear in the SQL. Pass both directly to a DB-API
        cursor: `cursor.execute(sql, params)`.

        Returns:
            A (sql, params) tuple.
        """
        ctx = RenderContext(self._compiler, parameterize=True)
        sql = self._render_sql(ctx)
        return sql, tuple(ctx.params)

    def limit(self, value: int) -> "QueryBuilder":
        """
        Specifies the maximum number of rows to return.
        Args:
            value (int): The maximum number of rows.
        Returns:
            QueryBuilder: The current QueryBuilder instance for chaining.
        """
        _validate_row_count(value, "LIMIT")
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
        _validate_row_count(value, "TOP")
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
        _validate_row_count(value, "OFFSET")
        if self._offset_value is not None:
            raise ValueError("Offset can only be set once per query.")
        self._offset_value = value
        return self

    def __getattr__(self, name: str) -> Callable[..., "QueryBuilder"]:
        """
        Dynamically handles method calls for joins, where clauses, and registered functions.
        """
        # Never resolve private or dunder names dynamically. Protocols such as
        # copy and pickle probe for these before __init__ has populated the
        # instance, and treating them as query methods causes infinite
        # recursion.
        if name.startswith("_"):
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )

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
        where_suffixes = "|".join(
            k for k in self._where_builder._WHERE_METHOD_MAP.keys()
        )
        where_match = re.match(
            rf"^(or|and)?({where_suffixes})$",
            name,
            re.IGNORECASE,
        )
        if where_match:

            def dynamic_caller(*args: Any, **kwargs: Any) -> "QueryBuilder":
                method_to_call = getattr(self._where_builder, name)
                method_to_call(*args, **kwargs)
                return self

            return dynamic_caller

        # Handle having methods by delegating to HavingClauseBuilder
        having_suffixes = "|".join(
            k.replace("where", "having")
            for k in self._where_builder._WHERE_METHOD_MAP.keys()
        )
        having_match = re.match(rf"^(or|and)?({having_suffixes})$", name, re.IGNORECASE)
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

        # Handle order by methods by delegating to OrderByClauseBuilder
        order_by_match = re.match(r"^orderBy$", name, re.IGNORECASE)
        if order_by_match:

            def dynamic_caller(*args: Any, **kwargs: Any) -> "QueryBuilder":
                method_to_call = getattr(self._order_by_builder, name)
                method_to_call(*args, **kwargs)
                return self

            return dynamic_caller

        # Handle registered functions dynamically
        try:
            # Use a case-insensitive check against the registry
            FunctionRegistry.get_metadata(name)

            def dynamic_func_caller(*args: Any, **kwargs: Any) -> "QueryBuilder":
                self.select_func(name, *args, **kwargs)
                return self

            return dynamic_func_caller
        except KeyError:
            # It's not a registered function, so continue to the final AttributeError
            pass

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )
