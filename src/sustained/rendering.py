"""
Rendering support for query builders.

A RenderContext carries the compiler and the value-handling mode through a
render pass. In inline mode, values are formatted as SQL literals. In
parameterized mode, each value is replaced with the dialect's placeholder
and collected in order so the caller can pass them to a database driver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, List, Optional, Sequence, Union

from sustained.types import Expression, SqlValue

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler


class RenderContext:
    """Carries rendering state through a single render pass."""

    def __init__(self, compiler: "Compiler", parameterize: bool = False) -> None:
        self.compiler = compiler
        self.parameterize = parameterize
        self.params: List[SqlValue] = []

    def value(self, value: SqlValue) -> str:
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


def split_value_markers(sql: str) -> List[str]:
    """
    Splits a SQL fragment on its ? value markers, the way str.split("?")
    would, except that a question mark inside a string literal or a quoted
    identifier is text and not a marker. Postgres jsonb operators and
    wildcards in strings both spell a literal question mark, and counting
    those as markers shifts every later value onto the wrong placeholder.

    Returns the literal pieces around the markers, so the marker count is
    the length minus one.
    """
    pieces: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    position = 0
    while position < len(sql):
        char = sql[position]
        if quote is not None:
            current.append(char)
            if char == quote:
                # A doubled quote inside a quoted region is an escaped
                # quote, not the end of the region.
                if position + 1 < len(sql) and sql[position + 1] == quote:
                    current.append(quote)
                    position += 1
                else:
                    quote = None
        elif char in ("'", '"', "`"):
            quote = char
            current.append(char)
        elif char == "?":
            pieces.append("".join(current))
            current = []
        else:
            current.append(char)
        position += 1
    pieces.append("".join(current))
    return pieces


def count_value_markers(sql: str) -> int:
    """The number of ? value markers in a fragment, quoted regions apart."""
    return len(split_value_markers(sql)) - 1


def bind_raw(sql: str, params: Sequence[SqlValue], ctx: RenderContext) -> str:
    """
    Renders a raw SQL fragment with ? value markers. Each marker becomes the
    dialect placeholder in parameterized mode or an inlined literal in
    inline mode. The marker count must match the parameter count.
    """
    pieces = split_value_markers(sql)
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
