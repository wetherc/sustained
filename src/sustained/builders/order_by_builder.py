from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple, Type

if TYPE_CHECKING:
    from ..compilers import Compiler
    from ..dialects import Dialects
    from ..model import Model


class OrderByClauseBuilder:
    """
    A builder for creating and managing ORDER BY clauses in a SQL query.
    """

    def __init__(
        self, model_class: Type["Model"], compiler: Optional["Compiler"] = None
    ):
        """
        Initializes the OrderByClauseBuilder.

        Args:
            model_class (Type[Model]): The Model class associated with this query.
        """
        self._model_class = model_class
        from ..dialects import Dialects  # Imported here to prevent circular dependency

        self._compiler = (
            compiler if compiler else Dialects.get_compiler(Dialects.DEFAULT)
        )
        self._clauses: List[Tuple[str, str]] = []

    def orderBy(self, column: str, direction: str = "asc") -> "OrderByClauseBuilder":
        """
        Adds an ORDER BY clause to the query.

        Args:
            column (str): The column to order by.
            direction (str, optional): The direction of ordering ('asc' or 'desc').
                                     Defaults to 'asc'.

        Returns:
            OrderByClauseBuilder: The builder instance for chaining.
        """
        normalized_direction = direction.upper()
        if normalized_direction not in ["ASC", "DESC"]:
            raise ValueError("Order by direction must be 'asc' or 'desc'.")

        self._clauses.append((column, normalized_direction))
        return self

    def __str__(self) -> str:
        """
        Builds and returns the final ORDER BY clause string.

        Returns:
            str: The complete ORDER BY clause, or an empty string if no clauses exist.
        """
        if not self._clauses:
            return ""

        clauses_str = ", ".join(
            [
                f"{self._compiler.quote_fully_qualified_identifier(col)} {direction}"
                for col, direction in self._clauses
            ]
        )
        return f"ORDER BY {clauses_str}"
