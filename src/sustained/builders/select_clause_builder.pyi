from typing import Optional

from ..compilers import Compiler
from ..types import Selectable

class SelectClauseBuilder:
    def __init__(self, compiler: Optional[Compiler] = None) -> None: ...
    def select(self, *columns: Selectable) -> None: ...
