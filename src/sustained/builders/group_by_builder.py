from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    List,
    Optional,
    Type,
)

if TYPE_CHECKING:
    from ..compilers import Compiler
    from ..dialects import Dialects
    from ..model import Model


class GroupByClauseBuilder:
    """A helper class for building GROUP BY clauses."""

    def __init__(
        self, model_class: Type["Model"], compiler: Optional["Compiler"] = None
    ):
        self._model_class = model_class
        from ..dialects import Dialects  # Imported here to prevent circular dependency

        self._compiler = (
            compiler if compiler else Dialects.get_compiler(Dialects.DEFAULT)
        )
        self._group_by_columns: List[str] = []
        self._mode: Optional[str] = None
        self._grouping_sets: Optional[List[tuple]] = None

    def groupBy(self, *columns: str) -> None:
        """Adds columns to the GROUP BY clause."""
        self._group_by_columns.extend(columns)

    def set_mode(self, mode: str, columns: List[str]) -> None:
        """Switches the clause to ROLLUP or CUBE over the given columns."""
        if not columns:
            raise ValueError(f"{mode} requires at least one column.")
        self._mode = mode
        self._group_by_columns = list(columns)

    def set_grouping_sets(self, sets: List[tuple]) -> None:
        """Switches the clause to explicit GROUPING SETS."""
        if not sets:
            raise ValueError("GROUPING SETS requires at least one set.")
        self._grouping_sets = sets

    def _quote(self, column: str) -> str:
        return self._compiler.quote_column_reference(column)

    def __str__(self) -> str:
        """Builds the final GROUP BY clause string."""
        if self._grouping_sets is not None:
            groups = ", ".join(
                "(" + ", ".join(self._quote(c) for c in group) + ")"
                for group in self._grouping_sets
            )
            return f"GROUP BY GROUPING SETS ({groups})"
        if not self._group_by_columns:
            return ""
        columns_sql = ", ".join(self._quote(c) for c in self._group_by_columns)
        if self._mode:
            return f"GROUP BY {self._mode} ({columns_sql})"
        return "GROUP BY " + columns_sql
