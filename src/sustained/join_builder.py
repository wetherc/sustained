from __future__ import annotations

from typing import List, Tuple


class JoinClauseBuilder:
    """
    A helper class for building complex JOIN ... ON clauses.
    An instance of this is passed to the lambda in `...join(..., lambda j: ...)` calls.
    """

    def __init__(self) -> None:
        self._conditions: List[Tuple[str, str]] = []

    def on(self, col1: str, op: str, col2: str) -> "JoinClauseBuilder":
        """Adds an ON condition. If this is not the first condition, it's treated as AND ON."""
        conjunction = "AND" if self._conditions else ""
        self._add_condition(conjunction, col1, op, col2)
        return self

    def andOn(self, col1: str, op: str, col2: str) -> "JoinClauseBuilder":
        """Adds an AND ON condition."""
        if not self._conditions:
            raise RuntimeError(
                "Cannot use 'andOn' for the first join condition. Use 'on' instead."
            )
        self._add_condition("AND", col1, op, col2)
        return self

    def orOn(self, col1: str, op: str, col2: str) -> "JoinClauseBuilder":
        """Adds an OR ON condition."""
        if not self._conditions:
            raise RuntimeError(
                "Cannot use 'orOn' for the first join condition. Use 'on' instead."
            )
        self._add_condition("OR", col1, op, col2)
        return self

    def _add_condition(self, conjunction: str, col1: str, op: str, col2: str) -> None:
        condition_str = f"{col1} {op} {col2}"
        self._conditions.append((conjunction, condition_str))

    def __str__(self) -> str:
        """Builds the final ON clause string."""
        if not self._conditions:
            raise RuntimeError("A join condition must be specified inside the lambda.")

        # The first condition doesn't have a preceding conjunction
        parts = [self._conditions[0][1]]
        for conjunction, clause in self._conditions[1:]:
            parts.append(f"{conjunction} {clause}")
        return " ".join(parts)
