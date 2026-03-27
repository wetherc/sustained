from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Type

from .types import CaseResult, RelationMapping

if TYPE_CHECKING:
    from .builder import QueryBuilder


class Model:
    """
    A base model class that mimics Objection.js models for defining database tables
    and their relationships.

    To use this, create a subclass and define the `tableName` and, optionally,
    `relationMappings`, `tableSchema`, and `database`.

    Attributes:
        database (str, optional): The name of the database. Defaults to None.
        tableName (str): The name of the table in the database. Defaults to None.
        tableSchema (str, optional): The schema of the table. Defaults to None.
        relationMappings (Dict[str, RelationMapping]): A dictionary defining
            relationships to other models.
    """

    database: Optional[str] = None
    tableName: Optional[str] = None
    tableSchema: Optional[str] = None
    relationMappings: Dict[str, RelationMapping] = {}

    def __init__(self, **kwargs: Any) -> None:
        """
        Initializes a model instance, allowing attributes to be set from
        keyword arguments.
        """
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self) -> str:
        """Provides a developer-friendly representation of the model instance."""
        attributes = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"{self.__class__.__name__}({attributes})"

    def __getattr__(self, name: str) -> str:
        """
        Provides attribute-style access to table columns, which returns a
        fully-qualified column name string for use in queries.

        Example:
            If a `User` model has `tableName = 'users'`, then `User().id` would
            return `'users.id'`.

        Raises:
            AttributeError: If the attribute does not exist or if `tableName`
                            is not defined on the model.
        """
        cls = self.__class__
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(f"'{cls.__name__}' object has no attribute '{name}'")

        database = getattr(cls, "database", None)
        table_schema = getattr(cls, "tableSchema", None)
        table_name = getattr(cls, "tableName", None)

        # We must have a table name to provide a column reference.
        if table_name:
            parts = []
            if database:
                parts.append(database)
            if table_schema:
                parts.append(table_schema)
            parts.append(table_name)
            parts.append(name)
            return ".".join(parts)

        raise AttributeError(f"'{cls.__name__}' object has no attribute '{name}'")

    @classmethod
    def query(cls) -> "QueryBuilder":
        """
        Starts a new query for this model.

        Returns:
            QueryBuilder: A new QueryBuilder instance for this model.
        """
        from .builder import QueryBuilder

        return QueryBuilder(cls)


def create_model(
    name: str,
    table_name: str,
    mappings: Optional[Dict[str, RelationMapping]] = None,
    table_schema: Optional[str] = None,
    database: Optional[str] = None,
) -> Type[Model]:
    """
    Dynamically creates a `Model` subclass.

    This is useful when you need to define models programmatically instead of
    declaratively.

    Args:
        name (str): The name of the new model class (e.g., "Animal").
        table_name (str): The database table name for the model.
        mappings (Dict[str, RelationMapping], optional): A dictionary of
            relation mappings. Defaults to None.
        table_schema (str, optional): The database schema. Defaults to None.
        database (str, optional): The database name. Defaults to None.

    Returns:
        Type[Model]: A new class that inherits from `Model`.

    Example:
        .. code-block:: python

            Person = create_model('Person', 'persons')

            Animal = create_model(
                'Animal',
                'animals',
                mappings={
                    'owner': {
                        'relation': RelationType.BelongsToOneRelation,
                        'modelClass': Person,
                        'join': {'from': 'animals.ownerId', 'to': 'persons.id'}
                    }
                }
            )

            query = Animal.query().select('name').joinRelated('owner')
    """
    if mappings is None:
        mappings = {}

    model_attrs = {"tableName": table_name, "relationMappings": mappings}

    if table_schema:
        model_attrs["tableSchema"] = table_schema
    if database:
        model_attrs["database"] = database

    return type(name, (Model,), model_attrs)
