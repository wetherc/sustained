"""
A Python query builder inspired by Objection.js.

This package provides a set of classes that allow you to build SQL queries
in a more programmatic and reusable way. The main components are:

- Model: A base class for defining database models and their relations.
- QueryBuilder: A class for constructing SQL queries.
- RelationType: An Enum for defining the type of relationship between models.
- create_model: A factory function for dynamically creating Model classes.
"""

from sustained.builder import QueryBuilder
from sustained.exceptions import (
    AmbiguousColumns,
    DialectError,
    GuardBlocked,
    MigrationError,
    RehearsalRequired,
)
from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Column,
    ColumnExpr,
    Func,
    Literal,
    Predicate,
    WindowExpression,
    col,
)
from sustained.model import Model, create_model
from sustained.types import (
    BasicJoinMapping,
    Binding,
    Connection,
    Cursor,
    Join,
    JoinMappingWithThrough,
    RelationMapping,
    RelationType,
    RowValue,
    SqlValue,
    ThroughJoinMapping,
    ThroughJoinValue,
)

__all__ = [
    # from exceptions
    "AmbiguousColumns",
    "DialectError",
    "GuardBlocked",
    "MigrationError",
    "RehearsalRequired",
    # from expressions
    "Column",
    "ColumnExpr",
    "Predicate",
    "col",
    "Func",
    "Literal",
    "AggregateExpression",
    "WindowExpression",
    "CaseExpression",
    # from types
    "RelationType",
    "Connection",
    "Cursor",
    "Binding",
    "SqlValue",
    "RowValue",
    "BasicJoinMapping",
    "ThroughJoinValue",
    "ThroughJoinMapping",
    "JoinMappingWithThrough",
    "Join",
    "RelationMapping",
    # from builder
    "QueryBuilder",
    # from model
    "Model",
    "create_model",
]
