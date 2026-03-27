from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Type, Union

if TYPE_CHECKING:
    from ..expressions import SelectExpression
    from ..model import Model


class SelectClauseBuilder:
    """
    Builds the SELECT clause of a SQL query.
    """

    def __init__(self, model_class: Type["Model"]):
        """
        Initializes the SelectClauseBuilder.

        Args:
            model_class (Type[Model]): The Model class associated with the query.
        """
        self._model_class = model_class
        self._selects: List[Union[str, "SelectExpression"]] = []

    def select(self, *columns: Union[str, "SelectExpression"]) -> None:
        """
        Adds columns or expressions to the SELECT clause.

        Args:
            *columns (Union[str, SelectExpression]): Columns or expressions to select.
        """
        self._selects.extend(columns)

    def __str__(self) -> str:
        """
        Generates the SQL string for the SELECT clause.

        Returns:
            str: The SELECT clause as a string.
        """
        if not self._selects:
            return "*"
        return ", ".join(map(str, self._selects))
