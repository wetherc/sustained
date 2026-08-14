"""
Rendering support for query builders.

A RenderContext carries the compiler and the value-handling mode through a
render pass. In inline mode, values are formatted as SQL literals. In
parameterized mode, each value is replaced with the dialect's placeholder
and collected in order so the caller can pass them to a database driver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List, Union

from sustained.types import Expression

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler


class RenderContext:
    """Carries rendering state through a single render pass."""

    def __init__(self, compiler: "Compiler", parameterize: bool = False) -> None:
        self.compiler = compiler
        self.parameterize = parameterize
        self.params: List[Any] = []

    def value(self, value: Any) -> str:
        """
        Renders a user-supplied value.

        Expression objects are raw SQL and are emitted verbatim in both
        modes. Other values become literals in inline mode or placeholders
        in parameterized mode.
        """
        if isinstance(value, Expression):
            return str(value)
        if self.parameterize:
            self.params.append(value)
            return self.compiler.placeholder()
        return self.compiler.format_value(value)


Renderable = Union[str, Callable[[RenderContext], str]]
"""A clause fragment: either a fixed string or a deferred render function."""


def bind_raw(sql: str, params: List[Any], ctx: RenderContext) -> str:
    """
    Renders a raw SQL fragment with ? value markers. Each marker becomes the
    dialect placeholder in parameterized mode or an inlined literal in
    inline mode. The marker count must match the parameter count.
    """
    pieces = sql.split("?")
    if len(pieces) - 1 != len(params):
        raise ValueError(
            f"Raw SQL fragment has {len(pieces) - 1} value markers "
            f"but {len(params)} parameters were given."
        )
    out = [pieces[0]]
    for piece, param in zip(pieces[1:], params):
        out.append(ctx.value(param))
        out.append(piece)
    return "".join(out)


def render_part(part: Renderable, ctx: RenderContext) -> str:
    """Renders a Renderable with the given context."""
    if callable(part):
        return part(ctx)
    return part
