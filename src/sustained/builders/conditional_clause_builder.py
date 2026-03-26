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
)

from ..types import Expression

if TYPE_CHECKING:
    from ..builder import QueryBuilder
    from ..model import Model


class ConditionalClauseBuilder(ABC):
    """An abstract base class for building conditional clauses like WHERE and HAVING."""

    def __init__(self, model_class: Type["Model"]):
        self._model_class = model_class
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
        suffixes = ["In", "NotIn", "Between", "NotBetween", "Exists", "NotExists"]
        match = re.match(
            rf"^(or|and)?({self._clause_type}(?:{'|'.join(suffixes)})?)$",
            name,
            re.IGNORECASE,
        )
        if match:
            conjunction_str, type_str = match.groups()

            if not self._clauses:
                if conjunction_str:
                    raise RuntimeError(
                        f"Cannot start a {self._clause_keyword.lower()} clause with '{conjunction_str}'."
                    )
                conjunction = ""
            else:
                conjunction = (conjunction_str or "and").upper()

            type_lower = type_str.lower()
            base_type_lower = self._clause_type.lower()

            def dynamic_caller(*args: Any) -> "ConditionalClauseBuilder":
                if type_lower == base_type_lower:
                    if len(args) == 1 and callable(args[0]):
                        self._add_internal(conjunction, args[0])
                    elif len(args) == 3:
                        self._add_internal(conjunction, *args)
                    else:
                        raise ValueError(
                            f"Invalid arguments for '{self._clause_keyword.lower()}' "
                            f"method. Use `{self._clause_keyword.lower()}(column, operator, value)` "
                            f"or `{self._clause_keyword.lower()}(lambda q: ...)`."
                        )
                elif type_lower == f"{base_type_lower}in":
                    self._add_in_internal(conjunction, "IN", *args)
                elif type_lower == f"{base_type_lower}notin":
                    self._add_in_internal(conjunction, "NOT IN", *args)
                elif type_lower == f"{base_type_lower}between":
                    self._add_between_internal(conjunction, "BETWEEN", *args)
                elif type_lower == f"{base_type_lower}notbetween":
                    self._add_between_internal(conjunction, "NOT BETWEEN", *args)
                elif type_lower == f"{base_type_lower}exists":
                    self._add_exists_internal(conjunction, "EXISTS", *args)
                elif type_lower == f"{base_type_lower}notexists":
                    self._add_exists_internal(conjunction, "NOT EXISTS", *args)
                return self

            return dynamic_caller

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _add_between_internal(
        self, conjunction: str, op: str, col: str, val1: Any, val2: Any
    ) -> None:
        """Internal handler for adding `BETWEEN` and `NOT BETWEEN` clauses."""
        formatted_val1 = (
            f"'{val1.replace("'", "''")}'" if isinstance(val1, str) else str(val1)
        )
        formatted_val2 = (
            f"'{val2.replace("'", "''")}'" if isinstance(val2, str) else str(val2)
        )
        clause = f"{col} {op} {formatted_val1} AND {formatted_val2}"
        self._clauses.append((conjunction, clause))

    def _add_exists_internal(self, conjunction: str, op: str, query: Any) -> None:
        """Internal handler for adding `EXISTS` and `NOT EXISTS` clauses."""
        from ..builder import QueryBuilder

        clause_str = ""
        if callable(query):
            sub_query = QueryBuilder(self._model_class)
            query(sub_query)
            clause_str = str(sub_query)
        elif isinstance(query, (str, QueryBuilder)):
            clause_str = str(query)
        else:
            raise ValueError(
                "Argument for exists must be a callable, string, or QueryBuilder instance."
            )

        clause = f"{op} ({clause_str})"
        self._clauses.append((conjunction, clause))

    def _build_clause(self, column: str, operator: str, value: Any) -> str:
        """Formats a single clause condition."""
        if isinstance(value, Expression):
            formatted_value = str(value)
        elif isinstance(value, str):
            escaped_value = value.replace("'", "''")
            formatted_value = f"'{escaped_value}'"
        else:
            formatted_value = value
        return f"{column} {operator} {formatted_value}"

    def _add_internal(
        self,
        conjunction: str,
        column_or_callable: Any,
        op: Optional[str] = None,
        val: Optional[Any] = None,
    ) -> None:
        """Internal handler for adding clauses."""
        if callable(column_or_callable):
            # Create a new instance of the concrete subclass for nesting
            temp_builder = type(self)(self._model_class)
            column_or_callable(temp_builder)
            if temp_builder.has_clauses():
                grouped_clause_str = temp_builder._build_clause_list_string()
                self._clauses.append((conjunction, f"({grouped_clause_str})"))
        else:
            if op is None:
                raise ValueError(
                    f"Operator must be provided for non-callable {self._clause_keyword.lower()} clause."
                )
            clause = self._build_clause(column_or_callable, op, val)
            self._clauses.append((conjunction, clause))

    def _add_in_internal(self, conjunction: str, op: str, col: str, vals: Any) -> None:
        """Internal handler for adding `IN` and `NOT IN` clauses."""
        from ..builder import QueryBuilder

        if isinstance(vals, list):
            formatted_values = []
            for v in vals:
                if isinstance(v, str):
                    formatted_values.append(f"'{v.replace("'", "''")}'")
                else:
                    formatted_values.append(str(v))
            values_str = ", ".join(formatted_values)
        elif isinstance(vals, (str, QueryBuilder)):
            values_str = str(vals)
        elif callable(vals):
            sub_query = QueryBuilder(self._model_class)
            vals(sub_query)
            values_str = str(sub_query)
        else:
            raise ValueError(
                "Argument for In/NotIn must be a list, a callable, string, or QueryBuilder instance."
            )

        clause = f"{col} {op} ({values_str})"
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
