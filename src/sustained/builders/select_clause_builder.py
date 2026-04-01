"""
Select-clause builder.
"""

from typing import TYPE_CHECKING, List, Optional

from sustained.expressions import Func
from sustained.types import Expression

if TYPE_CHECKING:
    from sustained.compilers import Compiler
    from sustained.dialects import Dialects
    from sustained.types import Selectable


class SelectClauseBuilder:
    """
    Manages the list of selected items for a SQL query.
    """

    def __init__(self, compiler: Optional["Compiler"] = None) -> None:
        from sustained.dialects import (
            Dialects,  # Imported here to prevent circular dependency
        )

        self._compiler = (
            compiler if compiler else Dialects.get_compiler(Dialects.DEFAULT)
        )
        self._selected_columns: List["Selectable"] = []

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

        formatted_columns = []
        for c in self._selected_columns:
            if isinstance(c, str):
                if c == "*":
                    formatted_columns.append(c)
                else:
                    formatted_columns.append(
                        self._compiler.quote_fully_qualified_identifier(c)
                    )
            elif isinstance(c, Func):
                formatted_columns.append(self._compiler.compile_function(c))
            elif isinstance(c, Expression):
                formatted_columns.append(str(c))
            else:
                # Should not happen with current type hints, but as a safeguard:
                formatted_columns.append(str(c))

        return ", ".join(formatted_columns)

    def select(self, *columns: "Selectable") -> None:
        """
        Adds one or more columns or expressions to the select list.

        Args:
            *columns: A list of columns or expression objects to select.
        """
        self._selected_columns.extend(columns)
