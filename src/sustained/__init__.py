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
from sustained.exceptions import DialectError
from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Column,
    Func,
    WindowExpression,
)
from sustained.model import Model, create_model
from sustained.types import (
    BasicJoinMapping,
    Join,
    JoinMappingWithThrough,
    RelationMapping,
    RelationType,
    ThroughJoinMapping,
    ThroughJoinValue,
)

__all__ = [
    # from exceptions
    "DialectError",
    # from expressions
    "Column",
    "Func",
    "AggregateExpression",
    "WindowExpression",
    "CaseExpression",
    # from types
    "RelationType",
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
