"""
Central registry for SQL functions and their dialect-specific support.
"""

from types import MappingProxyType
from typing import Dict, List, Mapping, NamedTuple

from sustained.dialects import Dialects

# The default for functions with one name everywhere. A NamedTuple shares
# one default object across every instance, so this is read-only.
_NO_ALTERNATE_NAMES: Mapping[Dialects, str] = MappingProxyType({})


class FunctionMetadata(NamedTuple):
    """
    Metadata for a registered SQL function.

    Attributes:
        supported_dialects: A list of dialects that support this function
            under its registered name.
        dialect_names: Alternate spellings per dialect. A dialect listed
            here passes validation and the function renders under the
            alternate name, e.g. NOW() becomes GETDATE() on MSSQL.
    """

    supported_dialects: List[Dialects]
    dialect_names: Mapping[Dialects, str] = _NO_ALTERNATE_NAMES


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
            Dialects.ATHENA,
            Dialects.MSSQL,
            Dialects.POSTGRES,
            Dialects.DUCKDB,
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
            FunctionMetadata(
                supported_dialects=[
                    Dialects.PRESTO,
                    Dialects.ATHENA,
                    Dialects.POSTGRES,
                    Dialects.DUCKDB,
                ]
            ),
        )
        self.register(
            "GETDATE",
            FunctionMetadata(
                supported_dialects=[Dialects.MSSQL],
                dialect_names={
                    Dialects.PRESTO: "NOW",
                    Dialects.ATHENA: "NOW",
                    Dialects.POSTGRES: "NOW",
                    Dialects.DUCKDB: "NOW",
                },
            ),
        )
        self.register(
            "NOW",
            FunctionMetadata(
                supported_dialects=[
                    Dialects.PRESTO,
                    Dialects.ATHENA,
                    Dialects.POSTGRES,
                    Dialects.DUCKDB,
                ],
                dialect_names={Dialects.MSSQL: "GETDATE"},
            ),
        )
        # LENGTH is a common scalar, but it is spelled LEN in T-SQL, so it
        # needs an alternate name instead of a place in the list above.
        self.register(
            "LENGTH",
            FunctionMetadata(
                supported_dialects=[
                    Dialects.DEFAULT,
                    Dialects.PRESTO,
                    Dialects.ATHENA,
                    Dialects.POSTGRES,
                    Dialects.DUCKDB,
                ],
                dialect_names={Dialects.MSSQL: "LEN"},
            ),
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

    def resolve_name(self, name: str, dialect: Dialects) -> str:
        """
        Returns the dialect's spelling of a function name. Unregistered
        names and dialects without an alternate spelling return the name
        uppercased.
        """
        upper = name.upper()
        metadata = self._functions.get(upper)
        if metadata is None:
            return upper
        return metadata.dialect_names.get(dialect, upper)

    def is_supported(self, name: str, dialect: Dialects) -> bool:
        """
        Reports whether a registered function is usable on the dialect,
        either natively or through an alternate spelling. Unregistered
        functions are always considered usable.
        """
        metadata = self._functions.get(name.upper())
        if metadata is None:
            return True
        return (
            dialect in metadata.supported_dialects or dialect in metadata.dialect_names
        )


# Create a singleton instance of the registry
FunctionRegistry = _FunctionRegistry()
