from typing import Optional

from ..compilers import Compiler
from ..rendering import RenderContext
from ..types import Selectable

class SelectClauseBuilder:
    def __init__(self, compiler: Optional[Compiler] = None) -> None: ...
    def render(self, ctx: RenderContext) -> str: ...
    def select(self, *columns: Selectable) -> None: ...
