from typing import TYPE_CHECKING, Any, Union

from ..types import DbReturnValue, Expression

if TYPE_CHECKING:
    from ..dialects import Dialects


class Compiler:
    def __init__(self, dialect: "Dialects") -> None:
        self._dialect = dialect

    def quote_identifier(self, identifier: str) -> str:
        return identifier

    def quote_fully_qualified_identifier(self, identifier: str) -> str:
        return identifier

    def placeholder(self) -> str:
        return "?"

    def format_value(self, value: Union[Expression, DbReturnValue]) -> str:
        if isinstance(value, Expression):
            return str(value)
        elif isinstance(value, str):
            escaped_value = value.replace("'", "''")
            return f"'{escaped_value}'"
        else:
            return str(value)
