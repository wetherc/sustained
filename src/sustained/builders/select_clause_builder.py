"""
Select-clause builder.
"""

from typing import Any, List


class SelectClauseBuilder:
    """
    Manages the list of selected items for a SQL query.
    """

    def __init__(self) -> None:
        self._selected_columns: List[Any] = []

    def __str__(self) -> str:
        """
        Generates the final column list for the SQL query.

        If no columns are selected, it defaults to '*'. Otherwise, it joins
        the selected columns, correctly handling both string and expression
        objects.

        Returns:
            The SQL fragment for the SELECT clause.
        """
        if not self._selected_columns:
            return "*"
        return ", ".join(str(c) for c in self._selected_columns)

    def select(self, *columns: Any) -> None:
        """
        Adds one or more columns or expressions to the select list.

        Args:
            *columns: A list of columns or expression objects to select.
        """
        self._selected_columns.extend(columns)
