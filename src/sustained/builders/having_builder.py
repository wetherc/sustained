from __future__ import annotations

from typing import TYPE_CHECKING

from .conditional_clause_builder import ConditionalClauseBuilder

if TYPE_CHECKING:
    from ..model import Model


class HavingClauseBuilder(ConditionalClauseBuilder):
    """A helper class for building complex HAVING clauses."""

    @property
    def _clause_keyword(self) -> str:
        return "HAVING"

    @property
    def _clause_type(self) -> str:
        return "Having"
