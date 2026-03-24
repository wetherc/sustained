"""
A Python query builder inspired by Objection.js.

This package provides a set of classes that allow you to build SQL queries
in a more programmatic and reusable way. The main components are:

- Model: A base class for defining database models and their relations.
- QueryBuilder: A class for constructing SQL queries.
- RelationType: An Enum for defining the type of relationship between models.
- create_model: A factory function for dynamically creating Model classes.
"""
from .objection import (
    RelationType,
    BasicJoinMapping,
    ThroughJoinValue,
    ThroughJoinMapping,
    JoinMappingWithThrough,
    Join,
    RelationMapping,
    QueryBuilder,
    Model,
    create_model,
)

__all__ = [
    'RelationType',
    'BasicJoinMapping',
    'ThroughJoinValue',
    'ThroughJoinMapping',
    'JoinMappingWithThrough',
    'Join',
    'RelationMapping',
    'QueryBuilder',
    'Model',
    'create_model',
]
