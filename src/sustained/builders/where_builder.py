from __future__ import annotations

import re
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    List,
    Optional,
    Tuple,
    Type,
)

if TYPE_CHECKING:
    from ..model import Model


class WhereClauseBuilder:
    """A helper class for building complex WHERE clauses."""

    def __init__(self, model_class: Type["Model"]):
        self._model_class = model_class
        self._where_clauses: List[Tuple[str, str]] = []

    def __getattr__(self, name: str) -> Callable[..., "WhereClauseBuilder"]:
        """
        Dynamically handles method calls for where clauses.
        """
        where_match = re.match(
            r"^(or|and)?(Where|WhereIn|WhereNotIn)$", name, re.IGNORECASE
        )
        if where_match:
            conjunction_str, where_type_str = where_match.groups()

            if not self._where_clauses:
                if conjunction_str:
                    raise RuntimeError(
                        f"Cannot start a where clause with '{conjunction_str}'."
                    )
                conjunction = ""
            else:
                conjunction = (conjunction_str or "and").upper()

            where_type = where_type_str.lower()

            def dynamic_where_caller(*args: Any) -> "WhereClauseBuilder":
                if where_type == "where":
                    if len(args) == 1 and callable(args[0]):
                        self._add_where_internal(conjunction, args[0])
                    elif len(args) == 3:
                        self._add_where_internal(conjunction, *args)
                    else:
                        raise ValueError(
                            "Invalid arguments for 'where' method. Use `where(column, operator, value)` or `where(lambda q: ...)`."
                        )
                elif where_type == "wherein":
                    self._add_where_in_internal(conjunction, "IN", *args)
                elif where_type == "wherenotin":
                    self._add_where_in_internal(conjunction, "NOT IN", *args)
                return self

            return dynamic_where_caller

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _build_where_clause(self, column: str, operator: str, value: Any) -> str:
        """Formats a single where clause condition."""
        formatted_value = value
        if isinstance(value, str):
            escaped_value = value.replace("'", "''")
            formatted_value = f"'{escaped_value}'"
        return f"{column} {operator} {formatted_value}"

    def _add_where_internal(
        self,
        conjunction: str,
        column_or_callable: Any,
        op: Optional[str] = None,
        val: Optional[Any] = None,
    ) -> None:
        """Internal handler for adding `where` clauses."""
        if callable(column_or_callable):
            temp_builder = WhereClauseBuilder(self._model_class)
            column_or_callable(temp_builder)
            if temp_builder.has_clauses():
                grouped_clause_str = temp_builder._build_clause_list_string()
                self._where_clauses.append((conjunction, f"({grouped_clause_str})"))
        else:
            if op is None:
                raise ValueError(
                    "Operator must be provided for non-callable where clause."
                )
            clause = self._build_where_clause(column_or_callable, op, val)
            self._where_clauses.append((conjunction, clause))

    def _add_where_in_internal(
        self, conjunction: str, op: str, col: str, vals: List[Any]
    ) -> None:
        """Internal handler for adding `WHERE IN` and `WHERE NOT IN` clauses."""
        formatted_values = []
        for v in vals:
            if isinstance(v, str):
                formatted_values.append(f"'{v.replace("'", "''")}'")
            else:
                formatted_values.append(str(v))
        values_str = ", ".join(formatted_values)
        clause = f"{col} {op} ({values_str})"
        self._where_clauses.append((conjunction, clause))

    def _build_clause_list_string(self) -> str:
        """Builds the complete WHERE clause string from all parts."""
        if not self._where_clauses:
            return ""

        # The first clause doesn't have a preceding conjunction
        parts = [self._where_clauses[0][1]]
        for conjunction, clause in self._where_clauses[1:]:
            parts.append(f"{conjunction} {clause}")
        return " ".join(parts)

    def __str__(self) -> str:
        """Builds the final WHERE clause string."""
        if not self._where_clauses:
            return ""
        return "WHERE " + self._build_clause_list_string()

    def has_clauses(self) -> bool:
        return bool(self._where_clauses)
