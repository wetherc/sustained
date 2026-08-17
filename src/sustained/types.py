from __future__ import annotations

import datetime
from decimal import Decimal
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Type,
    TypedDict,
    Union,
)

if TYPE_CHECKING:
    from .builder import QueryBuilder
    from .expressions import (
        AggregateExpression,
        CaseExpression,
        Column,
        ColumnExpr,
        Func,
        Subquery,
        WindowExpression,
    )
    from .model import Model
    from .pool import ConnectionPool


SqlValue = object
"""
A value on its way into the database, bound as a parameter or rendered as
a literal. Drivers accept whatever they can adapt, so there is no useful
set of types to name here. It is `object` and not `Any`, so passing one of
these values where a string or a number is wanted stays a type error.
"""

RowValue = Any
"""
A value read back out of the database. The driver picks the Python type,
and the same column can arrive as `bytes`, `Decimal`, or `str` depending
on which driver ran the query, so nothing narrower can be promised. This
one stays `Any` on purpose: rows are meant to be used, and `object` would
make every caller write a cast.
"""


ColumnDescription = Sequence[Any]
"""
One entry of a cursor's `description`. The DB-API fixes the first field as
the column name and leaves the other six to the driver, and some drivers
return an object of their own instead of a tuple, so position 0 is the only
part worth naming.
"""


class Cursor(Protocol):
    """
    The DB-API 2.0 cursor methods Sustained calls. Any driver's cursor
    satisfies it; nothing needs to subclass it.

    `parameters` is typed loosely because every driver declares its own
    adapted-parameter type, and a narrower annotation here would stop real
    driver cursors from matching the protocol.
    """

    @property
    def description(self) -> Optional[Sequence[ColumnDescription]]: ...

    @property
    def rowcount(self) -> int: ...

    def execute(self, operation: str, parameters: Sequence[Any] = ..., /) -> object: ...

    def executemany(
        self, operation: str, seq_of_parameters: Sequence[Sequence[Any]], /
    ) -> object: ...

    def fetchone(self) -> Optional[Sequence[RowValue]]: ...

    def fetchall(self) -> Sequence[Sequence[RowValue]]: ...

    def close(self) -> None: ...


class Connection(Protocol):
    """
    The DB-API 2.0 connection methods Sustained calls. Any driver's
    connection satisfies it; nothing needs to subclass it.
    """

    def cursor(self) -> Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


Binding = Union[Connection, "ConnectionPool"]
"""What Model.bind() accepts: one connection, or a pool that hands them out."""

RelationTree = Dict[str, "RelationTree"]
"""Dotted relation paths folded into a tree, so a shared prefix loads once."""

if TYPE_CHECKING:
    AnyQuery = QueryBuilder[Any]
    """
    A query builder whose model type is not fixed at this position, such as
    a subquery argument or a union operand. Python generics are invariant,
    so QueryBuilder[Model] would reject QueryBuilder[User]; Any is the only
    parameter that accepts a builder for every model.
    """


DbReturnValue = Union[
    str, int, float, bool, datetime.datetime, datetime.date, Decimal, bytes
]
"""
A literal value in a filter or a write: the Python types a driver binds as
a parameter without help. Timestamps, dates, decimals, and binary data are
in the set, because a column of that type is compared against a value of
that type. `None` is not: the clause methods that accept it say so with
`Optional`, so that a checker can hold apart `= NULL` from `IS NULL`.
"""

Selectable = Union[
    str,
    "AggregateExpression",
    "WindowExpression",
    "CaseExpression",
    "Column",
    "ColumnExpr",
    "Func",
    "Subquery",
]
CaseResult = Union[DbReturnValue, "Column"]
QueryResolvable = Union[Callable[..., "AnyQuery"], str, "AnyQuery"]
"""A subquery in argument position: a builder, a callable returning one, or SQL."""

WriteResult = Union[int, List[Dict[str, RowValue]]]
"""What a write returns: the affected row count, or the RETURNING rows."""


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
