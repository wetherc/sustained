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


class HavingClauseBuilder:
    """A helper class for building complex HAVING clauses."""

    def __init__(self, model_class: Type["Model"]):
        self._model_class = model_class
        self._having_clauses: List[Tuple[str, str]] = []

    def __getattr__(self, name: str) -> Callable[..., "HavingClauseBuilder"]:
        """
        Dynamically handles method calls for having clauses.
        """
        having_match = re.match(
            r"^(or|and)?(Having|HavingIn|HavingNotIn)$", name, re.IGNORECASE
        )
        if having_match:
            conjunction_str, having_type_str = having_match.groups()

            if not self._having_clauses:
                if conjunction_str:
                    raise RuntimeError(
                        f"Cannot start a having clause with '{conjunction_str}'."
                    )
                conjunction = ""
            else:
                conjunction = (conjunction_str or "and").upper()

            having_type = having_type_str.lower()

            def dynamic_having_caller(*args: Any) -> "HavingClauseBuilder":
                if having_type == "having":
                    if len(args) == 1 and callable(args[0]):
                        self._add_having_internal(conjunction, args[0])
                    elif len(args) == 3:
                        self._add_having_internal(conjunction, *args)
                    else:
                        raise ValueError(
                            "Invalid arguments for 'having' method. Use `having(column, operator, value)` or `having(lambda q: ...)`."
                        )
                elif having_type == "havingin":
                    self._add_having_in_internal(conjunction, "IN", *args)
                elif having_type == "havingnotin":
                    self._add_having_in_internal(conjunction, "NOT IN", *args)
                return self

            return dynamic_having_caller

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _build_having_clause(self, column: str, operator: str, value: Any) -> str:
        """Formats a single having clause condition."""
        formatted_value = value
        if isinstance(value, str):
            escaped_value = value.replace("'", "''")
            formatted_value = f"'{escaped_value}'"
        return f"{column} {operator} {formatted_value}"

    def _add_having_internal(
        self,
        conjunction: str,
        column_or_callable: Any,
        op: Optional[str] = None,
        val: Optional[Any] = None,
    ) -> None:
        """Internal handler for adding `having` clauses."""
        if callable(column_or_callable):
            temp_builder = HavingClauseBuilder(self._model_class)
            column_or_callable(temp_builder)
            if temp_builder.has_clauses():
                grouped_clause_str = temp_builder._build_clause_list_string()
                self._having_clauses.append((conjunction, f"({grouped_clause_str})"))
        else:
            if op is None:
                raise ValueError(
                    "Operator must be provided for non-callable having clause."
                )
            clause = self._build_having_clause(column_or_callable, op, val)
            self._having_clauses.append((conjunction, clause))

    def _add_having_in_internal(
        self, conjunction: str, op: str, col: str, vals: List[Any]
    ) -> None:
        """Internal handler for adding `HAVING IN` and `HAVING NOT IN` clauses."""
        formatted_values = []
        for v in vals:
            if isinstance(v, str):
                formatted_values.append(f"'{v.replace("'", "''")}'")
            else:
                formatted_values.append(str(v))
        values_str = ", ".join(formatted_values)
        clause = f"{col} {op} ({values_str})"
        self._having_clauses.append((conjunction, clause))

    def _build_clause_list_string(self) -> str:
        """Builds the complete HAVING clause string from all parts."""
        if not self._having_clauses:
            return ""

        # The first clause doesn't have a preceding conjunction
        parts = [self._having_clauses[0][1]]
        for conjunction, clause in self._having_clauses[1:]:
            parts.append(f"{conjunction} {clause}")
        return " ".join(parts)

    def __str__(self) -> str:
        """Builds the final HAVING clause string."""
        if not self._having_clauses:
            return ""
        return "HAVING " + self._build_clause_list_string()

    def has_clauses(self) -> bool:
        return bool(self._having_clauses)
