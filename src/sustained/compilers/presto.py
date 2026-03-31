from typing import TYPE_CHECKING

from .base import Compiler

if TYPE_CHECKING:
    from ..dialects import Dialects


class PrestoCompiler(Compiler):
    def __init__(self, dialect: "Dialects") -> None:
        super().__init__(dialect)

    def quote_identifier(self, identifier: str) -> str:
        return f'"{identifier}"'

    def quote_fully_qualified_identifier(self, identifier: str) -> str:
        return ".".join([f'"{part}"' for part in identifier.split(".")])
