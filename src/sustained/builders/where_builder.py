from __future__ import annotations

from typing import TYPE_CHECKING

from .conditional_clause_builder import ConditionalClauseBuilder

if TYPE_CHECKING:
    from ..model import Model


class WhereClauseBuilder(ConditionalClauseBuilder):
    """A helper class for building complex WHERE clauses."""

    @property
    def _clause_keyword(self) -> str:
        return "WHERE"

    @property
    def _clause_type(self) -> str:
        return "Where"
