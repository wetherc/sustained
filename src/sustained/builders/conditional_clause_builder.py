from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import (
    TYPE_CHECKING,
    Callable,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

from ..dialects import Dialects
from ..rendering import Renderable, RenderContext, render_part
from ..types import DbReturnValue, Expression, QueryResolvable, SqlValue

if TYPE_CHECKING:
    from ..compilers import Compiler
    from ..model import Model
    from ..types import AnyQuery


class ConditionalClauseBuilder(ABC):
    """An abstract base class for building conditional clauses like WHERE and HAVING."""

    _WHERE_METHOD_MAP = {
        "where": "_add_internal",
        "whereIn": "_add_in_internal",
        "whereNotIn": "_add_in_internal",
        "whereBetween": "_add_between_internal",
        "whereNotBetween": "_add_between_internal",
        "whereExists": "_add_exists_internal",
        "whereNotExists": "_add_exists_internal",
        "whereLike": "_add_like_internal",
        "whereILike": "_add_like_internal",
        "whereNull": "_add_null_internal",
        "whereNotNull": "_add_null_internal",
        "whereRaw": "_add_raw_internal",
    }

    def __init__(
        self, model_class: Type["Model"], compiler: Optional["Compiler"] = None
    ):
        self._model_class = model_class
        self._compiler = (
            compiler if compiler else Dialects.get_compiler(Dialects.DEFAULT)
        )
        self._clauses: List[Tuple[str, Renderable]] = []

    @property
    @abstractmethod
    def _clause_keyword(self) -> str: ...

    @property
    @abstractmethod
    def _clause_type(self) -> str: ...

    def _quote_column(self, column: str) -> str:
        """Quotes a column reference through the compiler.

        Anything that is not a plain (optionally dotted) identifier, such as
        an aggregate call in a HAVING clause, is passed through untouched.
        """
        return self._compiler.quote_column_reference(column)

    def __getattr__(self, name: str) -> Callable[..., "ConditionalClauseBuilder"]:
        """
        Dynamically handles method calls for clauses.
        """
        # Private names are never clause methods; see QueryBuilder.__getattr__.
        if name.startswith("_"):
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )

        base_name = re.sub(r"^(or|and)", "", name, flags=re.IGNORECASE)
        if not base_name:
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )
        lookup_name = base_name[0].lower() + base_name[1:]
        lookup_name = re.sub(r"^having", "where", lookup_name, flags=re.IGNORECASE)
        method_name = self._WHERE_METHOD_MAP.get(lookup_name)

        if method_name:
            conjunction_str = re.match(r"^(or|and)", name, flags=re.IGNORECASE)
            if conjunction_str:
                conjunction = conjunction_str.group(0).upper()
            else:
                conjunction = "AND" if self._clauses else ""

            # Check if this is the first clause and an "or" or "and" prefix was used
            if not self._clauses and conjunction in ("OR", "AND"):
                raise RuntimeError(
                    f"Cannot start a {self._clause_keyword.lower()} clause with '{conjunction.lower()}'."
                )

            internal_method = getattr(self, method_name)

            # A pass-through to the internal handler resolved above. The
            # typed overloads a caller sees live in the stub beside this file.
            def dynamic_caller(*args: SqlValue) -> "ConditionalClauseBuilder":
                if "not" in base_name.lower():
                    op_override = True  # Flag to indicate "NOT" version
                else:
                    op_override = False

                if "ilike" in base_name.lower():
                    op_like_override = "ILIKE"
                elif "like" in base_name.lower():
                    op_like_override = "LIKE"
                else:
                    op_like_override = None

                internal_method(
                    conjunction,
                    *args,
                    op_override=op_override,
                    op_like_override=op_like_override,
                )
                return self

            return dynamic_caller

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _add_between_internal(
        self,
        conjunction: str,
        col: str,
        val1: DbReturnValue,
        val2: DbReturnValue,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding `BETWEEN` and `NOT BETWEEN` clauses."""
        actual_op = "NOT BETWEEN" if op_override else "BETWEEN"
        quoted_col = self._quote_column(col)

        def render(ctx: RenderContext) -> str:
            return f"{quoted_col} {actual_op} {ctx.value(val1)} AND {ctx.value(val2)}"

        self._clauses.append((conjunction, render))

    def _add_exists_internal(
        self,
        conjunction: str,
        query: QueryResolvable,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding `EXISTS` and `NOT EXISTS` clauses."""
        from ..builder import QueryBuilder

        actual_op = "NOT EXISTS" if op_override else "EXISTS"

        sub_builder: Optional["AnyQuery"] = None
        raw_sql: Optional[str] = None
        if isinstance(query, QueryBuilder):
            sub_builder = query
        elif callable(query):
            sub_builder = QueryBuilder(
                self._model_class, dialect=self._compiler._dialect
            )
            query(sub_builder)
        elif isinstance(query, str):
            raw_sql = query
        else:
            raise ValueError(
                "Argument for exists must be a callable, string, or QueryBuilder instance."
            )

        def render(ctx: RenderContext) -> str:
            if sub_builder is not None:
                return f"{actual_op} ({sub_builder._render_sql(ctx)})"
            return f"{actual_op} ({raw_sql})"

        self._clauses.append((conjunction, render))

    def _add_like_internal(
        self,
        conjunction: str,
        col: str,
        pattern: str,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding `LIKE` and `ILIKE` clauses."""
        actual_op = op_like_override if op_like_override else "LIKE"
        quoted_col = self._quote_column(col)

        def render(ctx: RenderContext) -> str:
            return ctx.compiler.compile_like(quoted_col, ctx.value(pattern), actual_op)

        self._clauses.append((conjunction, render))

    def _add_raw_internal(
        self,
        conjunction: str,
        sql: str,
        params: Optional[Sequence[SqlValue]] = None,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """
        Internal handler for raw predicates with bound values. Values are
        marked with ? in the fragment and supplied separately, so they
        parameterize like every other clause.
        """
        from ..rendering import bind_raw

        bound_params = list(params) if params else []
        # Validate the marker count at build time so mistakes surface early.
        if sql.count("?") != len(bound_params):
            raise ValueError(
                f"Raw SQL fragment has {sql.count('?')} value markers "
                f"but {len(bound_params)} parameters were given."
            )

        def render(ctx: RenderContext) -> str:
            return f"({bind_raw(sql, bound_params, ctx)})"

        self._clauses.append((conjunction, render))

    def _add_null_internal(
        self,
        conjunction: str,
        col: str,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding `IS NULL` and `IS NOT NULL` clauses."""
        actual_op = "IS NOT NULL" if op_override else "IS NULL"
        clause = f"{self._quote_column(col)} {actual_op}"
        self._clauses.append((conjunction, clause))

    def _add_internal(
        self,
        conjunction: str,
        column_or_callable: Union[str, Callable[["ConditionalClauseBuilder"], None]],
        op: Optional[str] = None,
        val: Optional[Union[Expression, DbReturnValue]] = None,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding clauses."""
        from ..expressions import Predicate

        if isinstance(column_or_callable, Predicate):
            if op is not None or val is not None:
                raise ValueError(
                    "A Predicate carries its own operator and value; pass it "
                    "as the only argument."
                )
            predicate = column_or_callable
            self._clauses.append((conjunction, predicate.render))
            return
        if callable(column_or_callable):
            # Create a new instance of the concrete subclass for nesting
            temp_builder = type(self)(self._model_class, self._compiler)
            column_or_callable(temp_builder)
            if temp_builder.has_clauses():

                def render(ctx: RenderContext) -> str:
                    return f"({temp_builder._build_clause_list_string(ctx)})"

                self._clauses.append((conjunction, render))
        else:
            if op is None:
                raise ValueError(
                    f"Operator must be provided for non-callable {self._clause_keyword.lower()} clause."
                )
            operator = self._compiler.validate_operator(op)
            if val is None:
                if operator in ("=", "IS"):
                    self._add_null_internal(conjunction, column_or_callable)
                    return
                if operator in ("!=", "<>", "IS NOT"):
                    self._add_null_internal(
                        conjunction, column_or_callable, op_override=True
                    )
                    return
                raise ValueError(
                    f"Value must be provided for non-callable {self._clause_keyword.lower()} clause."
                )
            quoted_col = self._quote_column(column_or_callable)

            if operator in ("LIKE", "NOT LIKE", "ILIKE", "NOT ILIKE"):

                def render(ctx: RenderContext) -> str:
                    return ctx.compiler.compile_like(
                        quoted_col, ctx.value(val), operator
                    )

            else:

                def render(ctx: RenderContext) -> str:
                    return f"{quoted_col} {operator} {ctx.value(val)}"

            self._clauses.append((conjunction, render))

    def _add_in_internal(
        self,
        conjunction: str,
        col: str,
        vals: Union[List[DbReturnValue], QueryResolvable],
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding `IN` and `NOT IN` clauses."""
        from ..builder import QueryBuilder

        actual_op = "NOT IN" if op_override else "IN"
        quoted_col = self._quote_column(col)

        if isinstance(vals, list):
            if not vals:
                raise ValueError("IN/NOT IN requires a non-empty list of values.")
            value_list = list(vals)

            def render(ctx: RenderContext) -> str:
                values_str = ", ".join(ctx.value(v) for v in value_list)
                return f"{quoted_col} {actual_op} ({values_str})"

            self._clauses.append((conjunction, render))
            return

        sub_builder: Optional["AnyQuery"] = None
        raw_sql: Optional[str] = None
        if isinstance(vals, QueryBuilder):
            sub_builder = vals
        elif isinstance(vals, str):
            raw_sql = vals
        elif callable(vals):
            sub_builder = QueryBuilder(
                self._model_class, dialect=self._compiler._dialect
            )
            vals(sub_builder)
        else:
            raise ValueError(
                "Argument for In/NotIn must be a list, a callable, string, or QueryBuilder instance."
            )

        def render_sub(ctx: RenderContext) -> str:
            if sub_builder is not None:
                return f"{quoted_col} {actual_op} ({sub_builder._render_sql(ctx)})"
            return f"{quoted_col} {actual_op} ({raw_sql})"

        self._clauses.append((conjunction, render_sub))

    def _build_clause_list_string(self, ctx: RenderContext) -> str:
        """Builds the complete clause string from all parts."""
        if not self._clauses:
            return ""

        parts = [render_part(self._clauses[0][1], ctx)]
        for conjunction, clause in self._clauses[1:]:
            parts.append(f"{conjunction} {render_part(clause, ctx)}")
        return " ".join(parts)

    def render(self, ctx: RenderContext) -> str:
        """Builds the final clause string with the given context."""
        if not self._clauses:
            return ""
        return f"{self._clause_keyword} " + self._build_clause_list_string(ctx)

    def __str__(self) -> str:
        """Builds the final clause string with values inlined as literals."""
        return self.render(RenderContext(self._compiler))

    def has_clauses(self) -> bool:
        return bool(self._clauses)
