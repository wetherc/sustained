from typing import Type

from ..model import Model

class GroupByClauseBuilder:
    def __init__(self, model_class: Type[Model]) -> None: ...
    def groupBy(self, *columns: str) -> None: ...
