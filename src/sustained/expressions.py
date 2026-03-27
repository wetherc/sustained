from __future__ import annotations


class SelectExpression:
    """Base class for all selectable expressions."""

    def __str__(self) -> str:
        raise NotImplementedError("This method should be implemented by subclasses.")
