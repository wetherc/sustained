from typing import Optional, Type

from ..compilers import Compiler
from ..model import Model
from ..types import ColumnReference

class OrderByClauseBuilder:
    def __init__(
        self, model_class: Type[Model], compiler: Optional[Compiler] = None
    ) -> None: ...
    def orderBy(self, column: ColumnReference, direction: str = "ASC") -> None: ...
