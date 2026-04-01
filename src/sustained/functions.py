"""
Central registry for SQL functions and their dialect-specific support.
"""

from typing import Dict, List, NamedTuple

from .dialects import Dialects


class FunctionMetadata(NamedTuple):
    """
    Metadata for a registered SQL function.

    Attributes:
        supported_dialects: A list of dialects that support this function.
    """

    supported_dialects: List[Dialects]


class _FunctionRegistry:
    """
    A singleton registry for SQL function metadata.
    """

    def __init__(self) -> None:
        self._functions: Dict[str, FunctionMetadata] = {}
        self._register_default_functions()

    def _register_default_functions(self) -> None:
        """Pre-populates the registry with common SQL functions."""
        # Common aggregates supported by all dialects
        common_aggregates = ["COUNT", "SUM", "AVG", "MIN", "MAX"]
        for func_name in common_aggregates:
            self.register(
                func_name,
                FunctionMetadata(
                    supported_dialects=[
                        Dialects.DEFAULT,
                        Dialects.PRESTO,
                        Dialects.MSSQL,
                        Dialects.POSTGRES,
                    ]
                ),
            )

        # Dialect-specific functions
        self.register(
            "STRING_AGG",
            FunctionMetadata(supported_dialects=[Dialects.PRESTO, Dialects.POSTGRES]),
        )

    def register(self, name: str, metadata: FunctionMetadata) -> None:
        """
        Registers a new function or overwrites an existing one.
        """
        self._functions[name.upper()] = metadata

    def get_metadata(self, name: str) -> FunctionMetadata:
        """
        Retrieves metadata for a function.

        Args:
            name: The name of the function (case-insensitive).

        Returns:
            The FunctionMetadata for the function.

        Raises:
            KeyError: If the function is not registered.
        """
        return self._functions[name.upper()]


# Create a singleton instance of the registry
FunctionRegistry = _FunctionRegistry()
