from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Callable, Dict, Type, TypedDict, Union

if TYPE_CHECKING:
    from .builder import QueryBuilder
    from .expressions import (
        AggregateExpression,
        CaseExpression,
        Column,
        WindowExpression,
    )
    from .model import Model


DbReturnValue = Union[str, int, float, bool]
Selectable = Union[
    str, "AggregateExpression", "WindowExpression", "CaseExpression", "Column"
]
CaseResult = Union[DbReturnValue, "Column"]
QueryResolvable = Union[Callable[..., "QueryBuilder"], str, "QueryBuilder"]


class RelationType(Enum):
    """
    Defines the types of relations between models, mirroring Objection.js relation types.
    """

    BelongsToOneRelation = "BelongsToOneRelation"
    HasManyRelation = "HasManyRelation"
    HasOneRelation = "HasOneRelation"
    ManyToManyRelation = "ManyToManyRelation"


BasicJoinMapping = TypedDict(
    "BasicJoinMapping",
    {
        "from": str,
        "to": str,
    },
)
"""Defines a basic join between two tables."""

ThroughJoinValue = TypedDict(
    "ThroughJoinValue",
    {
        "table": Union[Type["Model"], str],
        "key": str,
    },
)
"""Specifies the intermediate table and key for a through relation."""

ThroughJoinMapping = TypedDict(
    "ThroughJoinMapping",
    {
        "from": ThroughJoinValue,
        "to": ThroughJoinValue,
    },
)
"""Defines the 'from' and 'to' parts of a 'through' clause in a many-to-many relation."""

JoinMappingWithThrough = TypedDict(
    "JoinMappingWithThrough",
    {
        "from": str,
        "through": ThroughJoinMapping,
        "to": str,
    },
)
"""Defines a many-to-many join that includes an intermediate 'through' table."""

Join = Union[BasicJoinMapping, JoinMappingWithThrough]
"""A union of possible join mapping types."""


class RelationMapping(TypedDict):
    """
    Describes a relationship between two models.

    Attributes:
        relation (RelationType): The type of the relation.
        modelClass (Union[Type["Model"], str]): The related model class or its name.
        join (Join): The join mapping that defines how the tables are connected.
    """

    relation: RelationType
    modelClass: Union[Type["Model"], str]
    join: Join


class Expression:
    """Represents a raw SQL expression that should not be quoted."""

    def __init__(self, value: str):
        self.value = value

    def __str__(self) -> str:
        return self.value
