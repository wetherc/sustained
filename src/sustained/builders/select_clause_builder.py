"""
Select-clause builder.
"""

import re
from typing import TYPE_CHECKING, List, Optional

from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Func,
    WindowExpression,
)
from sustained.types import Expression

_ALIAS_RE = re.compile(
    r"^(?P<column>.+?)\s+AS\s+(?P<alias>[A-Za-z_][A-Za-z0-9_$]*)$", re.IGNORECASE
)

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
                formatted_columns.append(self._format_string_column(c))
            elif isinstance(c, Func):
                formatted_columns.append(self._compiler.compile_function(c))
            elif isinstance(c, AggregateExpression):
                formatted_columns.append(self._compiler.compile_aggregate(c))
            elif isinstance(c, WindowExpression):
                formatted_columns.append(self._compiler.compile_window(c))
            elif isinstance(c, CaseExpression):
                formatted_columns.append(self._compiler.compile_case(c))
            elif isinstance(c, Expression):
                formatted_columns.append(str(c))
            else:
                # Subquery and Column render themselves.
                formatted_columns.append(str(c))

        return ", ".join(formatted_columns)

    def _format_string_column(self, column: str) -> str:
        """
        Formats a string column, supporting an optional 'col AS alias'
        suffix so aliased selections quote correctly in every dialect.
        """
        alias_match = _ALIAS_RE.match(column)
        if alias_match:
            quoted_column = self._compiler.quote_column_reference(
                alias_match.group("column").strip()
            )
            quoted_alias = self._compiler.quote_identifier(alias_match.group("alias"))
            return f"{quoted_column} AS {quoted_alias}"
        return self._compiler.quote_column_reference(column)

    def select(self, *columns: "Selectable") -> None:
        """
        Adds one or more columns or expressions to the select list.

        Args:
            *columns: A list of columns or expression objects to select.
        """
        self._selected_columns.extend(columns)
