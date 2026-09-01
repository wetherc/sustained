"""
Custom exceptions for the Sustained query builder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Sequence

if TYPE_CHECKING:
    from sustained.guards import Verdict


class SustainedError(Exception):
    """Base exception for all Sustained-related errors."""

    pass


class DialectError(SustainedError):
    """Raised when a feature is not supported by the current SQL dialect."""

    pass


class AmbiguousColumns(SustainedError):
    """
    Raised when a result set repeats a column name. `columns` holds the
    repeated names, in the order the result set returned them.
    """

    def __init__(self, columns: Sequence[str]) -> None:
        self.columns = list(columns)
        names = ", ".join(repr(c) for c in self.columns)
        super().__init__(
            f"This result set returns {names} more than once, usually from a "
            "join over tables that share a column name. A row keeps one "
            "value per name, so the others would be lost. Alias them in "
            "select(), such as select('users.id AS user_id', "
            "'accounts.id AS account_id')."
        )


class RehearsalRequired(SustainedError):
    """
    Raised when a run would apply SQL that removes data and no passing
    rehearsal covers that exact set of statements.
    """

    pass


class GuardBlocked(SustainedError):
    """
    Raised when a guard blocks a statement the run would apply. `verdicts`
    holds the blocking verdicts, in the order the guards returned them.
    """

    def __init__(self, verdicts: Sequence["Verdict"]) -> None:
        self.verdicts = list(verdicts)
        width = max(len(v.rule) for v in self.verdicts)
        lines = [f"  {v.rule:<{width}}  {v.statement}" for v in self.verdicts]
        super().__init__(
            "\n".join(
                ["A guard blocked this run:"]
                + lines
                + [
                    "Fix the statement, or take the rule out of the guard "
                    "list to run it anyway."
                ]
            )
        )


class MigrationError(SustainedError):
    """Raised when migration validation finds problems."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "Migration validation failed:\n"
            + "\n".join(f"- {p}" for p in self.problems)
        )
