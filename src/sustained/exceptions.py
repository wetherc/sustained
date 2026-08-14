"""
Custom exceptions for the Sustained query builder.
"""


class SustainedError(Exception):
    """Base exception for all Sustained-related errors."""

    pass


class DialectError(SustainedError):
    """Raised when a feature is not supported by the current SQL dialect."""

    pass


class MigrationError(SustainedError):
    """Raised when migration validation finds problems."""

    def __init__(self, problems: "list[str]") -> None:
        self.problems = list(problems)
        super().__init__(
            "Migration validation failed:\n"
            + "\n".join(f"- {p}" for p in self.problems)
        )
