from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple, Type, Union

from sustained.builder import QueryBuilder
from sustained.dialects import Dialects
from sustained.types import CaseResult, RelationMapping

if TYPE_CHECKING:
    pass


_MODEL_REGISTRY: Dict[str, Type["Model"]] = {}


def get_registered_model(name: str) -> Optional[Type["Model"]]:
    """Returns a previously defined Model subclass by class name, if any."""
    return _MODEL_REGISTRY.get(name)


def resolve_model_reference(
    reference: "Union[Type[Model], str]", context_module: Optional[str] = None
) -> Type["Model"]:
    """
    Resolves a model reference that may be a class or a class name.

    Names resolve through the model registry first. As a fallback for
    classes that never registered, the context module is searched.

    Raises:
        ValueError: If a string reference cannot be resolved.
    """
    if not isinstance(reference, str):
        return reference
    registered = get_registered_model(reference)
    if registered is not None:
        return registered
    if context_module:
        try:
            module = __import__(context_module, fromlist=[reference])
            return getattr(module, reference)  # type: ignore[no-any-return]
        except AttributeError:
            pass
    raise ValueError(
        f"Cannot resolve model reference '{reference}'. Define the model "
        "class before building the query, or pass the class itself instead "
        "of its name."
    )


def _qualified_column(cls: Type["Model"], name: str) -> str:
    """Builds the fully qualified column string for a model class."""
    parts = []
    if cls.database:
        parts.append(cls.database)
    if cls.tableSchema:
        parts.append(cls.tableSchema)
    assert cls.tableName is not None
    parts.append(cls.tableName)
    parts.append(name)
    return ".".join(parts)


def _check_declared_columns(cls: Type["Model"], name: str) -> None:
    """Raises if the model declares its columns and this name is not one."""
    if cls.columns is not None and name not in cls.columns:
        raise AttributeError(
            f"'{cls.__name__}' does not declare a column named '{name}'. "
            f"Declared columns: {', '.join(cls.columns)}."
        )


class ModelMeta(type):
    """
    Metaclass that registers Model subclasses by class name and provides
    column access on the class itself, so `User.id` works without
    instantiating the model.
    """

    def __init__(cls, name: str, bases: Tuple[type, ...], namespace: Dict[str, Any]):
        super().__init__(name, bases, namespace)
        if getattr(cls, "tableName", None):
            _MODEL_REGISTRY[name] = cls  # type: ignore[assignment]

    def __getattr__(cls, name: str) -> str:
        if name.startswith("_"):
            raise AttributeError(
                f"type object '{cls.__name__}' has no attribute '{name}'"
            )
        if getattr(cls, "tableName", None):
            _check_declared_columns(cls, name)  # type: ignore[arg-type]
            return _qualified_column(cls, name)  # type: ignore[arg-type]
        raise AttributeError(f"type object '{cls.__name__}' has no attribute '{name}'")


class Model(metaclass=ModelMeta):
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
    columns: Optional[Tuple[str, ...]] = None
    _dialect: Dialects = Dialects.DEFAULT
    _connection: Optional[Any] = None

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
        # Private and dunder names are never table columns. Refusing them here
        # keeps copy, pickle, and typo'd internals from resolving to bogus
        # column strings.
        if name.startswith("_"):
            raise AttributeError(f"'{cls.__name__}' object has no attribute '{name}'")

        # We must have a table name to provide a column reference.
        if cls.tableName:
            _check_declared_columns(cls, name)
            return _qualified_column(cls, name)

        raise AttributeError(f"'{cls.__name__}' object has no attribute '{name}'")

    @classmethod
    def set_dialect(cls, dialect: Dialects) -> None:
        """
        Sets the SQL dialect for all queries made with this model.

        Args:
            dialect: The dialect to use.
        """
        cls._dialect = dialect

    @classmethod
    def bind(cls, connection: Any) -> None:
        """
        Binds a DB-API 2.0 connection for queries made with this model.
        Binding on Model itself shares the connection with every model;
        binding on a subclass scopes it to that subclass and its children.

        Args:
            connection: An open DB-API 2.0 connection whose paramstyle
                matches the dialect (qmark by default, format for Postgres).
        """
        cls._connection = connection

    @classmethod
    def unbind(cls) -> None:
        """Removes the connection bound to this class, if any."""
        cls._connection = None

    @classmethod
    def query(cls) -> "QueryBuilder":
        """
        Starts a new query for this model.

        Returns:
            QueryBuilder: A new QueryBuilder instance for this model.
        """
        return QueryBuilder(cls, dialect=cls._dialect)


def create_model(
    name: str,
    table_name: str,
    mappings: Optional[Dict[str, RelationMapping]] = None,
    table_schema: Optional[str] = None,
    database: Optional[str] = None,
    columns: Optional[Tuple[str, ...]] = None,
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

    model_attrs: Dict[str, Any] = {
        "tableName": table_name,
        "relationMappings": mappings,
    }

    if table_schema:
        model_attrs["tableSchema"] = table_schema
    if database:
        model_attrs["database"] = database
    if columns is not None:
        model_attrs["columns"] = tuple(columns)

    return type(name, (Model,), model_attrs)
