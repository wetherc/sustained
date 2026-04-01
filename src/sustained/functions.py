"""
Central registry for SQL functions and their dialect-specific support.
"""

from typing import Dict, List, NamedTuple

from sustained.dialects import Dialects


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
        all_dialects = [
            Dialects.DEFAULT,
            Dialects.PRESTO,
            Dialects.MSSQL,
            Dialects.POSTGRES,
        ]

        # Common aggregates supported by all dialects
        common_aggregates = ["COUNT", "SUM", "AVG", "MIN", "MAX"]
        for func_name in common_aggregates:
            self.register(func_name, FunctionMetadata(supported_dialects=all_dialects))

        # Common scalar functions supported by all dialects
        common_scalars = [
            "LOWER",
            "UPPER",
            "COALESCE",
            "CONCAT",
            "SUBSTRING",
            "TRIM",
            "LENGTH",
            "ROUND",
            "ABS",
            "CEILING",
            "FLOOR",
        ]
        for func_name in common_scalars:
            self.register(func_name, FunctionMetadata(supported_dialects=all_dialects))

        # Dialect-specific functions
        self.register(
            "STRING_AGG",
            FunctionMetadata(supported_dialects=[Dialects.PRESTO, Dialects.POSTGRES]),
        )
        self.register("GETDATE", FunctionMetadata(supported_dialects=[Dialects.MSSQL]))
        self.register(
            "NOW",
            FunctionMetadata(supported_dialects=[Dialects.PRESTO, Dialects.POSTGRES]),
        )
        # The MOD function has different syntax across dialects, but we register the name
        # to allow for future custom renderers.
        self.register("MOD", FunctionMetadata(supported_dialects=all_dialects))

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
