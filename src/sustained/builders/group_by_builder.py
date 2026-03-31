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

    def groupBy(self, *columns: str) -> None:
        """Adds columns to the GROUP BY clause."""
        self._group_by_columns.extend(columns)

    def __str__(self) -> str:
        """Builds the final GROUP BY clause string."""
        if not self._group_by_columns:
            return ""
        return "GROUP BY " + ", ".join(
            self._compiler.quote_identifier(c) for c in self._group_by_columns
        )
