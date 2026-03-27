from typing import Type

from ..model import Model

class OrderByClauseBuilder:
    def __init__(self, model_class: Type[Model]) -> None: ...
    def orderBy(self, column: str, direction: str = "ASC") -> None: ...
