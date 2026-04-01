from typing import TYPE_CHECKING, Any, Optional, Union

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

    def compile_top(self, value: int) -> str:
        return ""

    def compile_limit_offset(self, limit: Optional[int], offset: Optional[int]) -> str:
        parts = []
        if limit is not None:
            parts.append(f"LIMIT {limit}")
        if offset is not None:
            parts.append(f"OFFSET {offset}")
        return " ".join(parts)
