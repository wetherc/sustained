from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    List,
    Optional,
    Tuple,
    Type,
    Union,
)

from ..dialects import Dialects
from ..types import DbReturnValue, Expression, QueryResolvable

if TYPE_CHECKING:
    from ..builder import QueryBuilder
    from ..compilers import Compiler
    from ..model import Model


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
    }

    def __init__(
        self, model_class: Type["Model"], compiler: Optional["Compiler"] = None
    ):
        self._model_class = model_class
        self._compiler = (
            compiler if compiler else Dialects.get_compiler(Dialects.DEFAULT)
        )
        self._clauses: List[Tuple[str, str]] = []

    @property
    @abstractmethod
    def _clause_keyword(self) -> str: ...

    @property
    @abstractmethod
    def _clause_type(self) -> str: ...

    def __getattr__(self, name: str) -> Callable[..., "ConditionalClauseBuilder"]:
        """
        Dynamically handles method calls for clauses.
        """
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

            def dynamic_caller(*args: Any) -> "ConditionalClauseBuilder":
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
        formatted_val1 = self._compiler.format_value(val1)
        formatted_val2 = self._compiler.format_value(val2)
        actual_op = "NOT BETWEEN" if op_override else "BETWEEN"
        clause = f"{col} {actual_op} {formatted_val1} AND {formatted_val2}"
        self._clauses.append((conjunction, clause))

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

        clause_str = ""
        if callable(query):
            sub_query = QueryBuilder(self._model_class, dialect=self._compiler._dialect)
            query(sub_query)
            clause_str = str(sub_query)
        elif isinstance(query, (str, QueryBuilder)):
            clause_str = str(query)
        else:
            raise ValueError(
                "Argument for exists must be a callable, string, or QueryBuilder instance."
            )

        clause = f"{actual_op} ({clause_str})"
        self._clauses.append((conjunction, clause))

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
        # Escape single quotes in the pattern for SQL literal
        formatted_pattern = self._compiler.format_value(pattern)
        clause = f"{col} {actual_op} {formatted_pattern}"
        self._clauses.append((conjunction, clause))

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
        clause = f"{col} {actual_op}"
        self._clauses.append((conjunction, clause))

    def _build_clause(
        self, column: str, operator: str, value: Union[Expression, DbReturnValue]
    ) -> str:
        """Formats a single clause condition."""
        formatted_value = self._compiler.format_value(value)
        return f"{column} {operator} {formatted_value}"

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
        if callable(column_or_callable):
            # Create a new instance of the concrete subclass for nesting
            temp_builder = type(self)(self._model_class, self._compiler)
            column_or_callable(temp_builder)
            if temp_builder.has_clauses():
                grouped_clause_str = temp_builder._build_clause_list_string()
                self._clauses.append((conjunction, f"({grouped_clause_str})"))
        else:
            if op is None:
                raise ValueError(
                    f"Operator must be provided for non-callable {self._clause_keyword.lower()} clause."
                )
            if val is None:
                raise ValueError(
                    f"Value must be provided for non-callable {self._clause_keyword.lower()} clause."
                )
            clause = self._build_clause(column_or_callable, op, val)
            self._clauses.append((conjunction, clause))

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

        if isinstance(vals, list):
            formatted_values = [self._compiler.format_value(v) for v in vals]
            values_str = ", ".join(formatted_values)
        elif isinstance(vals, (str, QueryBuilder)):
            values_str = str(vals)
        elif callable(vals):
            sub_query = QueryBuilder(self._model_class, dialect=self._compiler._dialect)
            vals(sub_query)
            values_str = str(sub_query)
        else:
            raise ValueError(
                "Argument for In/NotIn must be a list, a callable, string, or QueryBuilder instance."
            )

        clause = f"{col} {actual_op} ({values_str})"
        self._clauses.append((conjunction, clause))

    def _build_clause_list_string(self) -> str:
        """Builds the complete clause string from all parts."""
        if not self._clauses:
            return ""

        parts = [self._clauses[0][1]]
        for conjunction, clause in self._clauses[1:]:
            parts.append(f"{conjunction} {clause}")
        return " ".join(parts)

    def __str__(self) -> str:
        """Builds the final clause string."""
        if not self._clauses:
            return ""
        return f"{self._clause_keyword} " + self._build_clause_list_string()

    def has_clauses(self) -> bool:
        return bool(self._clauses)
