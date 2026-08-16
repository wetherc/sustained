from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    AsyncContextManager,
    ContextManager,
    Dict,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from sustained.builder import QueryBuilder
from sustained.dialects import Dialects
from sustained.types import Binding, CaseResult, Connection, RelationMapping

if TYPE_CHECKING:
    from sustained.aio import AsyncAdapter
    from sustained.expressions import ColumnExpr
    from sustained.schema import ColumnDef, Index, TableOptions


_MODEL_REGISTRY: Dict[str, Type["Model"]] = {}

TModel = TypeVar("TModel", bound="Model")


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


class ColumnNamespace:
    """
    Provides typed column access on a model class: Model.c.age returns a
    ColumnExpr that builds Predicate objects from comparison operators.
    """

    def __init__(self, model_class: Type["Model"]) -> None:
        self._model_class = model_class

    def __getattr__(self, name: str) -> "ColumnExpr":
        from sustained.expressions import ColumnExpr

        cls = self._model_class
        if name.startswith("_") or not cls.tableName:
            raise AttributeError(f"'{cls.__name__}.c' has no column named '{name}'")
        _check_declared_columns(cls, name)
        return ColumnExpr(_qualified_column(cls, name))


class ModelMeta(type):
    """
    Metaclass that registers Model subclasses by class name and provides
    column access on the class itself, so `User.id` works without
    instantiating the model.
    """

    @property
    def c(cls) -> ColumnNamespace:
        """Typed column namespace: Model.c.age is a ColumnExpr."""
        return ColumnNamespace(cls)  # type: ignore[arg-type]

    # The class namespace mixes methods, column definitions, and plain
    # attributes, exactly as type.__init__ receives it.
    def __init__(cls, name: str, bases: Tuple[type, ...], namespace: Dict[str, Any]):
        super().__init__(name, bases, namespace)
        if getattr(cls, "tableName", None):
            _MODEL_REGISTRY[name] = cls  # type: ignore[assignment]
        # A typed schema also declares the column names, so strict column
        # access comes along unless the class opts out explicitly.
        if namespace.get("tableColumns") and "columns" not in namespace:
            cls.columns = tuple(namespace["tableColumns"])

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
    tableColumns: Optional[Dict[str, "ColumnDef"]] = None
    indexes: Optional[list["Index"]] = None
    tableOptions: Optional["TableOptions"] = None
    _dialect: Dialects = Dialects.DEFAULT
    _connection: Optional[Binding] = None
    _async_adapter: Optional["AsyncAdapter"] = None

    # Hydration sets one attribute per selected column, and the column set
    # is only known at query time, so the row values cannot be typed here.
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
    def bind(cls, connection: Binding) -> None:
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
    def bind_async(cls, adapter: "AsyncAdapter") -> None:
        """
        Binds an AsyncAdapter for async queries made with this model. See
        sustained.aio for the shipped adapters.
        """
        cls._async_adapter = adapter

    @classmethod
    def unbind_async(cls) -> None:
        """Removes the async adapter bound to this class, if any."""
        cls._async_adapter = None

    @classmethod
    def async_transaction(
        cls, adapter: Optional["AsyncAdapter"] = None
    ) -> "AsyncContextManager[AsyncAdapter]":
        """
        Opens an async transaction context on the bound adapter, or the one
        passed in. Statements inside the block share one transaction that
        commits on success and rolls back on an exception. Nested blocks
        use savepoints, so an inner failure rolls back only the inner block.
        """
        from sustained.aio import async_transaction

        resolved = adapter if adapter is not None else cls._async_adapter
        if resolved is None:
            raise RuntimeError(
                "No async adapter. Bind one with Model.bind_async(adapter) "
                "or pass it to async_transaction()."
            )
        return async_transaction(resolved)

    @classmethod
    def _qualified_table_sql(cls) -> str:
        from sustained.dialects import Dialects

        compiler = Dialects.get_compiler(cls._dialect)
        parts = []
        if cls.database:
            parts.append(compiler.quote_identifier(cls.database))
        if cls.tableSchema:
            parts.append(compiler.quote_identifier(cls.tableSchema))
        if not cls.tableName:
            raise ValueError(f"Model '{cls.__name__}' must define a tableName.")
        parts.append(compiler.quote_identifier(cls.tableName))
        return ".".join(parts)

    @classmethod
    def create_table_sql(cls, if_not_exists: bool = False) -> str:
        """
        Renders the CREATE TABLE statement for this model's tableColumns
        using the model's dialect.
        """
        from sustained.dialects import Dialects
        from sustained.schema import build_create_table_sql

        if not cls.tableColumns:
            raise ValueError(
                f"Model '{cls.__name__}' has no tableColumns to create a table from."
            )
        compiler = Dialects.get_compiler(cls._dialect)
        return build_create_table_sql(
            compiler,
            cls._qualified_table_sql(),
            cls.tableColumns,
            if_not_exists,
            options=cls.tableOptions,
        )

    @classmethod
    def create_indexes_sql(cls) -> list[str]:
        """Renders CREATE INDEX statements for the model's indexes."""
        from sustained.dialects import Dialects

        compiler = Dialects.get_compiler(cls._dialect)
        statements = []
        for index in cls.indexes or []:
            statements.append(
                compiler.compile_create_index(
                    index.name,
                    cls._qualified_table_sql(),
                    list(index.columns),
                    index.unique,
                )
            )
        return statements

    @classmethod
    def create_table_statements(cls, if_not_exists: bool = False) -> list[str]:
        """The CREATE TABLE statement followed by its CREATE INDEX statements."""
        return [
            cls.create_table_sql(if_not_exists=if_not_exists)
        ] + cls.create_indexes_sql()

    @classmethod
    def create_table(
        cls, connection: Optional[Binding] = None, if_not_exists: bool = False
    ) -> None:
        """
        Executes CREATE TABLE for this model on the connection, followed by
        the model's CREATE INDEX statements.
        """
        from sustained.execution import connection_scope

        with connection_scope(connection, cls._connection) as conn:
            cursor = conn.cursor()
            for statement in cls.create_table_statements(if_not_exists=if_not_exists):
                cursor.execute(statement)

    @classmethod
    def drop_table_sql(cls, if_exists: bool = True) -> str:
        """Renders the DROP TABLE statement for this model."""
        exists_sql = "IF EXISTS " if if_exists else ""
        return f"DROP TABLE {exists_sql}{cls._qualified_table_sql()}"

    @classmethod
    def drop_table(
        cls, connection: Optional[Binding] = None, if_exists: bool = True
    ) -> None:
        """Executes DROP TABLE for this model on the connection."""
        from sustained.execution import connection_scope

        with connection_scope(connection, cls._connection) as conn:
            conn.cursor().execute(cls.drop_table_sql(if_exists=if_exists))

    @classmethod
    def transaction(
        cls, connection: Optional[Binding] = None
    ) -> ContextManager[Connection]:
        """
        Opens a transaction context on the bound connection, or on the one
        passed in. Statements inside the block share one transaction that
        commits on success and rolls back on an exception. Nested blocks use
        savepoints.

        Example:
            with User.transaction():
                User.query().insert({...}).run()
                Account.query().update({...}).where(...).run()
        """
        from sustained.execution import transaction

        conn = connection if connection is not None else cls._connection
        if conn is None:
            raise RuntimeError(
                "No database connection. Bind one with Model.bind(connection) "
                "or pass it to transaction()."
            )
        return transaction(conn)

    @classmethod
    def query(cls: Type[TModel]) -> "QueryBuilder[TModel]":
        """
        Starts a new query for this model.

        Returns:
            QueryBuilder: A new QueryBuilder for this model. A type checker
                reads it as QueryBuilder[cls], so run() gives List[cls].
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

    model_attrs: Dict[str, object] = {
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
