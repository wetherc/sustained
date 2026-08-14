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
    Predicate,
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
        self._with_clauses: List[Tuple[str, "QueryBuilder", bool]] = []
        self._offset_value: Optional[int] = None
        self._union_clauses: List[Tuple[str, "QueryBuilder"]] = []
        self._limit_value: Optional[int] = None
        self._top_value: Optional[int] = None
        self._from_source: Optional[Union[str, Tuple["QueryBuilder", str]]] = None
        self._distinct = False
        self._stmt_type = "select"
        self._insert_rows: List[Dict[str, Any]] = []
        self._update_values: Dict[str, Any] = {}
        self._returning_columns: List[str] = []
        self._eager_relations: List[str] = []
        self._conflict_columns: Optional[List[str]] = None
        self._conflict_action: Optional[Tuple[str, Optional[List[str]]]] = None
        self._insert_from: Optional[Tuple[Optional[List[str]], "QueryBuilder"]] = None
        self._ctas_target: Tuple[str, bool] = ("", False)
        self._distinct_on_columns: List[str] = []
        self._qualify_condition: Optional[Union[str, "Predicate"]] = None
        self._locking_clause: Optional[Tuple[bool, bool]] = None

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
        if not FunctionRegistry.is_supported(function_name, self._dialect):
            raise DialectError(
                f"Function '{function_name.upper()}' is not supported by the '{self._dialect.name}' dialect."
            )

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
        args: Optional[List[Any]] = None,
        frame: Optional[str] = None,
    ) -> "QueryBuilder":
        """
        Adds a window function to the select clause.

        Args:
            function_name: The name of the window function (e.g., 'ROW_NUMBER').
            alias: The alias for the resulting column.
            partition_by: A list of columns to partition by.
            order_by: A list of columns to order by. Entries may carry a
                direction suffix, e.g. 'created_at DESC'.
            args: Arguments for the function itself, e.g. the column for LAG.
                Strings are column references; wrap literals in Literal().
            frame: An optional frame clause such as
                'ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW'.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        self._validate_function(function_name)
        window = WindowExpression(
            function_name, alias, partition_by, order_by, args=args, frame=frame
        )
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

    def with_(
        self, table_alias: str, subquery: "QueryBuilder", recursive: bool = False
    ) -> "QueryBuilder":
        """
        Adds a Common Table Expression (CTE) to the query. Pass
        recursive=True for a self-referencing CTE; the WITH clause then
        renders as WITH RECURSIVE on dialects that require the keyword.
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
        self._with_clauses.append((table_alias, subquery, recursive))
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
            try:
                full_table_name = self._model_table_sql()
            except ValueError:
                # A model without a table renders a FROM-less SELECT.
                full_table_name = ""

        joins_str = str(self._join_builder)
        where_str = self._where_builder.render(ctx)
        group_by_str = str(self._group_by_builder)
        having_str = self._having_builder.render(ctx)

        select_parts = ["SELECT"]
        if self._distinct:
            select_parts.append("DISTINCT")
        if self._distinct_on_columns:
            select_parts.append(
                self._compiler.compile_distinct_on(
                    [
                        self._compiler.quote_column_reference(c)
                        for c in self._distinct_on_columns
                    ]
                )
            )

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

        if self._qualify_condition is not None:
            if not self._compiler.supports_qualify():
                raise DialectError(
                    f"QUALIFY is not supported by the '{self._dialect.name}' dialect. "
                    "Wrap the window function in a subquery instead."
                )
            if isinstance(self._qualify_condition, Predicate):
                condition_sql = self._qualify_condition.render(ctx)
            else:
                condition_sql = str(self._qualify_condition)
            query_parts.append(f"QUALIFY {condition_sql}")

        return " ".join(query_parts)

    def _collect_ctes(self) -> List[Tuple[str, "QueryBuilder", bool]]:
        """
        Gathers CTEs from this query, its FROM subquery chain, its own CTE
        subqueries, and its union members, in dependency order. All of them
        are rendered in a single top-level WITH clause because WITH cannot
        appear inside a parenthesized subquery in every dialect.
        """
        ctes: List[Tuple[str, "QueryBuilder", bool]] = []
        if isinstance(self._from_source, tuple):
            ctes.extend(self._from_source[0]._collect_ctes())
        for alias, subquery, recursive in self._with_clauses:
            ctes.extend(subquery._collect_ctes())
            ctes.append((alias, subquery, recursive))
        for _, query in self._union_clauses:
            ctes.extend(query._collect_ctes())
        return ctes

    def _render_sql(self, ctx: RenderContext, include_ctes: bool = True) -> str:
        """Renders the full statement with the given context."""
        if self._stmt_type == "ctas":
            table_name, temporary = self._ctas_target
            table_sql = self._compiler.quote_fully_qualified_identifier(table_name)
            select_sql = self._render_select(ctx, include_ctes)
            return self._compiler.compile_ctas(table_sql, select_sql, temporary)
        if self._stmt_type != "select":
            return self._render_dml(ctx)
        return self._render_select(ctx, include_ctes)

    def _render_select(self, ctx: RenderContext, include_ctes: bool = True) -> str:
        """Renders the SELECT statement body."""
        query_parts = []

        if include_ctes:
            collected = self._collect_ctes()
            if collected:
                unique_ctes: Dict[str, "QueryBuilder"] = {}
                any_recursive = False
                for alias, subquery, recursive in collected:
                    any_recursive = any_recursive or recursive
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
                with_keyword = self._compiler.compile_with_keyword(any_recursive)
                query_parts.append(f"{with_keyword} " + ", ".join(cte_strs))

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

        if self._locking_clause is not None:
            if self._union_clauses:
                raise ValueError("FOR UPDATE cannot be combined with union queries.")
            skip_locked, nowait = self._locking_clause
            query_parts.append(self._compiler.compile_locking(skip_locked, nowait))

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

    def intersect(self, *queries: "QueryBuilder") -> "QueryBuilder":
        """
        Adds one or more INTERSECT clauses to the query, keeping only rows
        present in every query.
        """
        for q in queries:
            self._union_clauses.append(("INTERSECT", q))
        return self

    def except_(self, *queries: "QueryBuilder") -> "QueryBuilder":
        """
        Adds one or more EXCEPT clauses to the query, removing rows that
        appear in the given queries. Named except_ because except is a
        Python keyword.
        """
        for q in queries:
            self._union_clauses.append(("EXCEPT", q))
        return self

    def distinctOn(self, *columns: str) -> "QueryBuilder":
        """
        Adds a Postgres-style DISTINCT ON clause, keeping the first row of
        each group defined by the columns. Combine with orderBy() starting
        with the same columns. Supported on Postgres and DuckDB.
        """
        if not columns:
            raise ValueError("distinctOn() requires at least one column.")
        if self._distinct:
            raise ValueError("Cannot combine distinct() with distinctOn().")
        self._distinct_on_columns = list(columns)
        return self

    def groupByRollup(self, *columns: str) -> "QueryBuilder":
        """Groups by ROLLUP of the columns, adding subtotal rows."""
        self._group_by_builder.set_mode("ROLLUP", list(columns))
        return self

    def groupByCube(self, *columns: str) -> "QueryBuilder":
        """Groups by CUBE of the columns, adding every subtotal combination."""
        self._group_by_builder.set_mode("CUBE", list(columns))
        return self

    def groupByGroupingSets(self, *sets: Tuple[str, ...]) -> "QueryBuilder":
        """
        Groups by explicit GROUPING SETS. Each argument is a tuple of
        columns; an empty tuple is the grand total row.
        """
        self._group_by_builder.set_grouping_sets([tuple(s) for s in sets])
        return self

    def qualify(self, condition: Union[str, Predicate]) -> "QueryBuilder":
        """
        Adds a QUALIFY clause, which filters on window function results
        without a wrapping subquery. Supported on DuckDB.

        Args:
            condition: A Predicate or a raw SQL string.
        """
        self._qualify_condition = condition
        return self

    def for_update(
        self, skip_locked: bool = False, nowait: bool = False
    ) -> "QueryBuilder":
        """
        Appends FOR UPDATE row locking, optionally with SKIP LOCKED or
        NOWAIT. Supported on Postgres; not valid with union queries.
        """
        if skip_locked and nowait:
            raise ValueError("SKIP LOCKED and NOWAIT are mutually exclusive.")
        self._locking_clause = (skip_locked, nowait)
        return self

    def total(self, connection: Optional[Any] = None) -> int:
        """
        Executes SELECT COUNT(*) over this query with ORDER BY, LIMIT, and
        OFFSET stripped, and returns the row count. The query itself is
        left unmodified.
        """
        inner = self.clone()
        inner._order_by_builder = OrderByClauseBuilder(
            self._model_class, compiler=self._compiler
        )
        inner._limit_value = None
        inner._offset_value = None
        inner._top_value = None
        wrapper = QueryBuilder(self._model_class, dialect=self._dialect)
        wrapper.from_(inner, alias="sustained_count")
        wrapper.count("*", alias="total")
        return int(wrapper.to_dicts(connection)[0]["total"])

    def cursor_page(
        self, column: str, page_size: int, after: Optional[Any] = None
    ) -> "QueryBuilder":
        """
        Applies keyset pagination on a single column: orders by the column,
        filters rows greater than the last seen value, and limits to the
        page size. Scales better than OFFSET pagination on large tables.

        Args:
            column: The unique, ordered column to paginate on.
            page_size: Rows per page.
            after: The last value from the previous page, or None for the
                first page.
        """
        _validate_row_count(page_size, "PAGE SIZE")
        self.orderBy(column)
        if after is not None:
            self.where(column, ">", after)
        self.limit(page_size)
        return self

    def explain(
        self, connection: Optional[Any] = None, analyze: bool = False
    ) -> List[Tuple[Any, ...]]:
        """
        Runs the dialect's EXPLAIN on this query and returns the plan rows.
        With analyze=True the statement actually executes, so do not use it
        on writes you do not want applied.
        """
        import time

        from sustained.execution import notify_statement

        conn = self._resolve_connection(connection)
        sql, params = self.to_sql()
        prefix = self._compiler.compile_explain(analyze)
        cursor = conn.cursor()
        started = time.perf_counter()
        cursor.execute(f"{prefix} {sql}", params)
        notify_statement(f"{prefix} {sql}", params, time.perf_counter() - started)
        return list(cursor.fetchall())

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

    def _model_table_sql(self) -> str:
        """Renders the model's qualified, quoted table name."""
        model_cls = self._model_class
        parts = []
        if model_cls.database:
            parts.append(self._compiler.quote_identifier(model_cls.database))
        if model_cls.tableSchema:
            parts.append(self._compiler.quote_identifier(model_cls.tableSchema))
        if model_cls.tableName:
            parts.append(self._compiler.quote_identifier(model_cls.tableName))
        if not parts:
            raise ValueError(
                f"Model '{model_cls.__name__}' must define a tableName to build this statement."
            )
        return ".".join(parts)

    def insert(
        self, values: Union[Dict[str, Any], List[Dict[str, Any]]]
    ) -> "QueryBuilder":
        """
        Turns this query into an INSERT statement.

        Args:
            values: A dict of column-to-value pairs, or a list of such dicts
                for a multi-row insert. Every row must have the same columns.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        rows = values if isinstance(values, list) else [values]
        if not rows:
            raise ValueError("insert() requires at least one row.")
        first_keys = list(rows[0].keys())
        if not first_keys:
            raise ValueError("insert() rows must have at least one column.")
        for row in rows:
            if list(row.keys()) != first_keys:
                raise ValueError(
                    "All rows in a multi-row insert must share the same columns."
                )
        self._stmt_type = "insert"
        self._insert_rows = [dict(row) for row in rows]
        return self

    def insert_from(
        self, columns: Optional[List[str]], query: "QueryBuilder"
    ) -> "QueryBuilder":
        """
        Turns this query into an INSERT ... SELECT statement.

        Args:
            columns: The target columns, or None to insert positionally.
            query: The SELECT query providing the rows.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        if not isinstance(query, QueryBuilder):
            raise TypeError("insert_from() requires a QueryBuilder as the source.")
        self._stmt_type = "insert_from"
        self._insert_from = (list(columns) if columns else None, query)
        return self

    def create_table_as(
        self, table_name: str, temporary: bool = False
    ) -> "QueryBuilder":
        """
        Turns this SELECT into a CREATE TABLE ... AS statement (CTAS).

        Args:
            table_name: The name of the table to create.
            temporary: Create a temporary table when True.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        if self._stmt_type != "select":
            raise ValueError("create_table_as() applies to SELECT queries.")
        if not table_name:
            raise ValueError("create_table_as() requires a table name.")
        self._stmt_type = "ctas"
        self._ctas_target = (table_name, temporary)
        return self

    def onConflict(self, *columns: str) -> "QueryBuilder":
        """
        Declares the conflict target for an upsert. Follow with merge() or
        ignore(). Only valid after insert(), and every conflict column must
        be one of the inserted columns.

        Args:
            *columns: The unique or primary key columns that define a
                conflict.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        if self._stmt_type != "insert":
            raise ValueError("onConflict() applies to insert() statements.")
        if not columns:
            raise ValueError("onConflict() requires at least one column.")
        insert_columns = set(self._insert_rows[0].keys())
        missing = [c for c in columns if c not in insert_columns]
        if missing:
            raise ValueError(
                f"Conflict columns must be inserted columns; missing: {missing}."
            )
        self._conflict_columns = list(columns)
        return self

    def merge(self, columns: Optional[List[str]] = None) -> "QueryBuilder":
        """
        On conflict, updates the existing row. Updates every inserted
        column except the conflict columns, or only the columns given.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        if self._conflict_columns is None:
            raise ValueError("merge() requires onConflict() first.")
        self._conflict_action = ("merge", list(columns) if columns else None)
        return self

    def ignore(self) -> "QueryBuilder":
        """
        On conflict, skips the conflicting row instead of raising.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        if self._conflict_columns is None:
            raise ValueError("ignore() requires onConflict() first.")
        self._conflict_action = ("ignore", None)
        return self

    def update(self, values: Dict[str, Any]) -> "QueryBuilder":
        """
        Turns this query into an UPDATE statement. Combine with where()
        clauses to target rows.

        Args:
            values: A dict of column-to-value pairs to set.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        if not values:
            raise ValueError("update() requires at least one column to set.")
        self._stmt_type = "update"
        self._update_values = dict(values)
        return self

    def delete(self) -> "QueryBuilder":
        """
        Turns this query into a DELETE statement. Combine with where()
        clauses to target rows.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        self._stmt_type = "delete"
        return self

    def returning(self, *columns: str) -> "QueryBuilder":
        """
        Adds a RETURNING clause to an INSERT, UPDATE, or DELETE statement on
        dialects that support it.

        Args:
            *columns: The columns to return. Defaults to '*' when omitted.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        self._returning_columns = list(columns) if columns else ["*"]
        return self

    def _render_dml(self, ctx: RenderContext) -> str:
        """Renders an INSERT, UPDATE, or DELETE statement."""
        table_sql = self._model_table_sql()

        if self._stmt_type == "insert_from":
            if self._where_builder.has_clauses():
                raise ValueError("INSERT statements cannot have a WHERE clause.")
            assert self._insert_from is not None
            source_columns, source_query = self._insert_from
            columns_part = ""
            if source_columns:
                quoted = ", ".join(
                    self._compiler.quote_identifier(c) for c in source_columns
                )
                columns_part = f" ({quoted})"
            select_sql = source_query._render_sql(ctx)
            sql = f"INSERT INTO {table_sql}{columns_part} {select_sql}"
            if self._returning_columns:
                returning_sql = ", ".join(
                    self._compiler.quote_column_reference(c)
                    for c in self._returning_columns
                )
                sql += f" {self._compiler.compile_returning(returning_sql)}"
            return sql

        if self._stmt_type == "insert":
            if self._where_builder.has_clauses():
                raise ValueError("INSERT statements cannot have a WHERE clause.")
            columns = list(self._insert_rows[0].keys())
            columns_sql = ", ".join(self._compiler.quote_identifier(c) for c in columns)
            row_groups = []
            for row in self._insert_rows:
                rendered = ", ".join(ctx.value(row[c]) for c in columns)
                row_groups.append(f"({rendered})")
            if self._conflict_columns is not None:
                if self._conflict_action is None:
                    raise ValueError(
                        "onConflict() needs an action: call merge() or ignore()."
                    )
                action, update_columns = self._conflict_action
                if action == "merge":
                    if update_columns is None:
                        update_columns = [
                            c for c in columns if c not in self._conflict_columns
                        ]
                    if not update_columns:
                        raise ValueError(
                            "merge() has no columns to update; every inserted "
                            "column is a conflict column."
                        )
                sql = self._compiler.compile_upsert_statement(
                    table_sql,
                    columns,
                    row_groups,
                    self._conflict_columns,
                    action,
                    update_columns or [],
                )
            else:
                sql = (
                    f"INSERT INTO {table_sql} ({columns_sql}) "
                    f"VALUES {', '.join(row_groups)}"
                )
        elif self._stmt_type == "update":
            if not self._where_builder.has_clauses():
                raise ValueError(
                    "UPDATE without a WHERE clause would modify every row. "
                    "Add where() clauses, or where(QueryBuilder.raw('1'), '=', 1) to force it."
                )
            # Assignments render before the WHERE clause so parameters are
            # collected in the order they appear in the SQL.
            assignments = ", ".join(
                f"{self._compiler.quote_identifier(c)} = {ctx.value(v)}"
                for c, v in self._update_values.items()
            )
            where_str = self._where_builder.render(ctx)
            sql = f"UPDATE {table_sql} SET {assignments} {where_str}"
        else:
            if not self._where_builder.has_clauses():
                raise ValueError(
                    "DELETE without a WHERE clause would remove every row. "
                    "Add where() clauses, or where(QueryBuilder.raw('1'), '=', 1) to force it."
                )
            where_str = self._where_builder.render(ctx)
            sql = f"DELETE FROM {table_sql} {where_str}"

        if self._returning_columns:
            returning_sql = ", ".join(
                self._compiler.quote_column_reference(c)
                for c in self._returning_columns
            )
            sql += f" {self._compiler.compile_returning(returning_sql)}"
        return sql

    def clone(self) -> "QueryBuilder":
        """
        Returns an independent deep copy of this query. Later changes to the
        copy do not affect the original, which makes shared base queries
        practical.
        """
        import copy

        return copy.deepcopy(self)

    def withGraphFetched(self, *relation_names: str) -> "QueryBuilder":
        """
        Eager loads the named relations when the query runs. Each relation
        costs one extra query; results attach to the fetched instances under
        the relation name. Through relations are not supported yet.

        Args:
            *relation_names: Relation names defined in relationMappings.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        for name in relation_names:
            if name not in self._model_class.relationMappings:
                raise ValueError(
                    f"Relation '{name}' not found in model "
                    f"'{self._model_class.__name__}'"
                )
            self._eager_relations.append(name)
        return self

    def _resolve_connection(self, connection: Optional[Any]) -> Any:
        conn = connection if connection is not None else self._model_class._connection
        if conn is None:
            raise RuntimeError(
                "No database connection. Bind one with Model.bind(connection) "
                "or pass it to run()."
            )
        return conn

    def _run_select_raw(self, connection: Optional[Any]) -> Tuple[List[str], List[Any]]:
        """Executes this SELECT and returns (column names, raw rows)."""
        import time

        from sustained.execution import notify_statement

        if self._stmt_type != "select":
            raise ValueError("Only SELECT queries return result sets.")
        conn = self._resolve_connection(connection)
        cursor = conn.cursor()
        sql, params = self.to_sql()
        started = time.perf_counter()
        cursor.execute(sql, params)
        notify_statement(sql, params, time.perf_counter() - started)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        return columns, cursor.fetchall()

    def to_dicts(self, connection: Optional[Any] = None) -> List[Dict[str, Any]]:
        """
        Executes the SELECT and returns rows as plain dicts keyed by column
        name. Eager loading is not applied; use run() for model instances.
        """
        columns, rows = self._run_select_raw(connection)
        return [dict(zip(columns, row)) for row in rows]

    def to_df(self, connection: Optional[Any] = None) -> Any:
        """
        Executes the SELECT and returns a pandas DataFrame with the query's
        column names. Requires pandas to be installed.
        """
        try:
            import pandas
        except ImportError:
            raise RuntimeError(
                "to_df() requires pandas. Install it with: pip install pandas"
            ) from None
        columns, rows = self._run_select_raw(connection)
        return pandas.DataFrame.from_records(list(rows), columns=columns)

    def to_arrow(self, connection: Optional[Any] = None) -> Any:
        """
        Executes the SELECT and returns a pyarrow Table with the query's
        column names. Requires pyarrow to be installed.
        """
        try:
            import pyarrow
        except ImportError:
            raise RuntimeError(
                "to_arrow() requires pyarrow. Install it with: pip install pyarrow"
            ) from None
        columns, rows = self._run_select_raw(connection)
        data = {name: [row[i] for row in rows] for i, name in enumerate(columns)}
        return pyarrow.table(data)

    def run(self, connection: Optional[Any] = None) -> Any:
        """
        Executes the query against a DB-API 2.0 connection.

        SELECT statements return a list of hydrated model instances, with
        any withGraphFetched() relations attached. Use to_dicts(), to_df(),
        or to_arrow() for other result shapes. INSERT, UPDATE, and DELETE
        statements are committed and return the affected row count, or a
        list of dicts when a RETURNING clause is present.

        Args:
            connection: A DB-API 2.0 connection. Falls back to the one
                bound with Model.bind().

        Returns:
            The statement's result, as described above.
        """
        import time

        from sustained.execution import (
            eager_load_relation,
            fetch_models,
            in_transaction,
            notify_statement,
        )

        conn = self._resolve_connection(connection)
        cursor = conn.cursor()

        use_executemany = (
            self._stmt_type == "insert"
            and len(self._insert_rows) > 1
            and not self._returning_columns
        )
        started = time.perf_counter()
        if use_executemany:
            # Render a single-row template and bind each row's values, so
            # large inserts go through the driver's batch path.
            template = self.clone()
            template._insert_rows = [self._insert_rows[0]]
            sql, _ = template.to_sql()
            columns = list(self._insert_rows[0].keys())
            row_params = [tuple(row[c] for c in columns) for row in self._insert_rows]
            cursor.executemany(sql, row_params)
            notify_statement(sql, (), time.perf_counter() - started)
        else:
            sql, params = self.to_sql()
            cursor.execute(sql, params)
            notify_statement(sql, params, time.perf_counter() - started)

        if self._stmt_type == "select":
            models = fetch_models(self._model_class, cursor)
            for relation_name in self._eager_relations:
                eager_load_relation(self._model_class, conn, models, relation_name)
            return models

        if self._returning_columns and cursor.description is not None:
            columns = [desc[0] for desc in cursor.description]
            result: Any = [dict(zip(columns, row)) for row in cursor.fetchall()]
        else:
            result = cursor.rowcount
        # Inside a transaction() context the context manager owns the
        # commit; committing here would break atomicity.
        if not in_transaction(conn) and hasattr(conn, "commit"):
            conn.commit()
        return result

    def first(self, connection: Optional[Any] = None) -> Optional["Model"]:
        """
        Executes the query with LIMIT 1 and returns the first hydrated model
        instance, or None when the result set is empty. The query itself is
        left unmodified.

        Args:
            connection: A DB-API 2.0 connection. Falls back to the one
                bound with Model.bind().

        Returns:
            The first model instance or None.
        """
        query = self.clone()
        if query._limit_value is None and query._top_value is None:
            query.limit(1)
        results = query.run(connection)
        return results[0] if results else None

    def page(self, page: int, page_size: int) -> "QueryBuilder":
        """
        Applies pagination as LIMIT and OFFSET. Pages are zero-based, so
        page(0, 25) returns the first 25 rows.

        Args:
            page: The zero-based page number.
            page_size: The number of rows per page.

        Returns:
            The current QueryBuilder instance for chaining.
        """
        _validate_row_count(page, "PAGE")
        _validate_row_count(page_size, "PAGE SIZE")
        self.limit(page_size)
        self.offset(page * page_size)
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

        # Accept snake_case spellings of the camelCase query methods, e.g.
        # where_in -> whereIn and order_by -> orderBy. Real methods such as
        # select_func never reach __getattr__, so they are unaffected.
        if "_" in name:
            camel_name = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), name)
            if camel_name != name:
                try:
                    return cast(
                        Callable[..., "QueryBuilder"], getattr(self, camel_name)
                    )
                except AttributeError:
                    raise AttributeError(
                        f"'{type(self).__name__}' object has no attribute '{name}'"
                    ) from None

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
