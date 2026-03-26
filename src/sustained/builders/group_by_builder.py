from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    List,
    Type,
)

if TYPE_CHECKING:
    from ..model import Model


class GroupByClauseBuilder:
    """A helper class for building GROUP BY clauses."""

    def __init__(self, model_class: Type["Model"]):
        self._model_class = model_class
        self._group_by_columns: List[str] = []

    def group_by(self, *columns: str) -> None:
        """Adds columns to the GROUP BY clause."""
        self._group_by_columns.extend(columns)

    def __str__(self) -> str:
        """Builds the final GROUP BY clause string."""
        if not self._group_by_columns:
            return ""
        return "GROUP BY " + ", ".join(self._group_by_columns)
