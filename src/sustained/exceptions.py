"""
Custom exceptions for the Sustained query builder.
"""


class SustainedError(Exception):
    """Base exception for all Sustained-related errors."""

    pass


class DialectError(SustainedError):
    """Raised when a feature is not supported by the current SQL dialect."""

    pass
