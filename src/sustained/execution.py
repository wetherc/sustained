"""
Execution support: running built queries against a DB-API 2.0 connection
and hydrating rows into model instances.

Bind a connection once with Model.bind(connection), or pass one to run()
per call. Any DB-API 2.0 connection works as long as its paramstyle matches
the dialect's placeholder: qmark for the default and MSSQL dialects
(sqlite3, pyodbc) and format for Postgres (psycopg, psycopg2).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Type,
)

from sustained.types import RelationType

if TYPE_CHECKING:
    from sustained.model import Model


# Connections with an open transaction, keyed by id(). The value holds a
# strong reference to the connection, so the id cannot be reused while the
# entry exists, plus the current savepoint nesting depth.
_ACTIVE_TRANSACTIONS: Dict[int, Tuple[Any, int]] = {}

# Optional observer called after every executed statement.
_statement_listener: Optional[Callable[[str, Tuple[Any, ...], float], None]] = None


def set_statement_listener(
    listener: Optional[Callable[[str, Tuple[Any, ...], float], None]],
) -> None:
    """
    Registers a callable invoked after every statement run() executes, with
    the SQL text, the parameter tuple, and the duration in seconds. Pass
    None to remove the listener. Useful for logging and timing.
    """
    global _statement_listener
    _statement_listener = listener


def notify_statement(sql: str, params: Tuple[Any, ...], duration: float) -> None:
    """Invokes the registered statement listener, if any."""
    if _statement_listener is not None:
        _statement_listener(sql, params, duration)


def in_transaction(connection: Any) -> bool:
    """Reports whether the connection has an open transaction() context."""
    entry = _ACTIVE_TRANSACTIONS.get(id(connection))
    return entry is not None and entry[0] is connection


@contextmanager
def transaction(connection: Any) -> Iterator[Any]:
    """
    Runs the block atomically on the connection. Commits when the block
    finishes and rolls back when it raises. While the context is open,
    run() stops committing per statement.

    Nested contexts on the same connection use ANSI savepoints, so an inner
    failure rolls back only the inner block.
    """
    key = id(connection)
    entry = _ACTIVE_TRANSACTIONS.get(key)

    if entry is not None and entry[0] is connection:
        depth = entry[1] + 1
        _ACTIVE_TRANSACTIONS[key] = (connection, depth)
        savepoint = f"sustained_sp_{depth}"
        connection.cursor().execute(f"SAVEPOINT {savepoint}")
        try:
            yield connection
            connection.cursor().execute(f"RELEASE SAVEPOINT {savepoint}")
        except BaseException:
            connection.cursor().execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            raise
        finally:
            _ACTIVE_TRANSACTIONS[key] = (connection, depth - 1)
        return

    _ACTIVE_TRANSACTIONS[key] = (connection, 0)
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        del _ACTIVE_TRANSACTIONS[key]


def fetch_models(model_class: Type["Model"], cursor: Any) -> List["Model"]:
    """Hydrates every row on the cursor into instances of model_class."""
    if cursor.description is None:
        return []
    columns = [desc[0] for desc in cursor.description]
    return [model_class(**dict(zip(columns, row))) for row in cursor.fetchall()]


def _split_column_ref(ref: str, relation_name: str) -> Tuple[str, str]:
    """Splits 'table.column' and rejects unqualified references."""
    if "." not in ref:
        raise ValueError(
            f"Relation '{relation_name}' join references must be qualified "
            f"as 'table.column', got {ref!r}."
        )
    table, column = ref.rsplit(".", 1)
    return table, column


def eager_load_relation(
    model_class: Type["Model"],
    connection: Any,
    parents: List["Model"],
    relation_name: str,
) -> None:
    """
    Loads a relation for a list of parent instances with one extra query and
    attaches the results to each parent under the relation name.

    HasManyRelation attaches a list; the to-one relation types attach a
    single instance or None. Through relations are not supported yet.
    """
    if not parents:
        return

    relation = model_class.relationMappings.get(relation_name)
    if not relation:
        raise ValueError(
            f"Relation '{relation_name}' not found in model '{model_class.__name__}'"
        )
    join_info = relation["join"]

    from sustained.model import resolve_model_reference

    related_cls = resolve_model_reference(
        relation["modelClass"], context_module=model_class.__module__
    )

    from_table, from_col = _split_column_ref(join_info["from"], relation_name)
    to_table, to_col = _split_column_ref(join_info["to"], relation_name)

    if "through" in join_info:
        _eager_load_through(
            model_class,
            related_cls,
            connection,
            parents,
            relation_name,
            relation,
            from_col,
            to_col,
        )
        return

    # The side whose table matches the parent model holds the parent key.
    if from_table == model_class.tableName:
        parent_col, child_col = from_col, to_col
    else:
        parent_col, child_col = to_col, from_col

    parent_keys = _collect_parent_keys(parents, parent_col, relation_name)
    unique_keys = [k for k in dict.fromkeys(parent_keys) if k is not None]

    is_many = relation["relation"] == RelationType.HasManyRelation
    if not unique_keys:
        for parent in parents:
            setattr(parent, relation_name, [] if is_many else None)
        return

    children = related_cls.query().whereIn(child_col, unique_keys).run(connection)

    grouped: Dict[Any, List["Model"]] = {}
    for child in children:
        if child_col not in child.__dict__:
            raise ValueError(
                f"Cannot eager load '{relation_name}': related rows do not "
                f"include the '{child_col}' column."
            )
        grouped.setdefault(child.__dict__[child_col], []).append(child)

    for parent, key in zip(parents, parent_keys):
        matches = grouped.get(key, [])
        if is_many:
            setattr(parent, relation_name, matches)
        else:
            setattr(parent, relation_name, matches[0] if matches else None)


def _collect_parent_keys(
    parents: List["Model"], parent_col: str, relation_name: str
) -> List[Any]:
    """
    Reads the join key from each parent's hydrated data. Values come from
    __dict__ so a missing column is an error instead of silently resolving
    to a qualified column string.
    """
    parent_keys = []
    for parent in parents:
        if parent_col not in parent.__dict__:
            raise ValueError(
                f"Cannot eager load '{relation_name}': parent rows were not "
                f"fetched with the '{parent_col}' column."
            )
        parent_keys.append(parent.__dict__[parent_col])
    return parent_keys


# Reserved alias for the parent join key in through-relation queries.
_PARENT_KEY_ALIAS = "sustained_parent_key"


def _eager_load_through(
    model_class: Type["Model"],
    related_cls: Type["Model"],
    connection: Any,
    parents: List["Model"],
    relation_name: str,
    relation: Any,
    parent_col: str,
    related_col: str,
) -> None:
    """
    Loads a many-to-many relation with one query that joins the related
    table to the through table and exposes the parent key under a reserved
    alias for grouping.
    """
    join_info = relation["join"]
    through = join_info["through"]

    def through_table_name(ref: Any) -> str:
        if isinstance(ref, str):
            return ref
        name = ref.tableName
        assert name is not None, "Through table model must have a tableName"
        return str(name)

    through_table = through_table_name(through["from"]["table"])
    through_from_key = through["from"]["key"]
    through_to_key = through["to"]["key"]
    related_table = related_cls.tableName
    assert related_table is not None

    parent_keys = _collect_parent_keys(parents, parent_col, relation_name)
    unique_keys = [k for k in dict.fromkeys(parent_keys) if k is not None]
    if not unique_keys:
        for parent in parents:
            setattr(parent, relation_name, [])
        return

    children = (
        related_cls.query()
        .select(
            f"{related_table}.*",
            f"{through_table}.{through_from_key} AS {_PARENT_KEY_ALIAS}",
        )
        .join(
            through_table,
            f"{through_table}.{through_to_key}",
            "=",
            f"{related_table}.{related_col}",
        )
        .whereIn(f"{through_table}.{through_from_key}", unique_keys)
        .run(connection)
    )

    grouped: Dict[Any, List["Model"]] = {}
    for child in children:
        key = child.__dict__.pop(_PARENT_KEY_ALIAS, None)
        grouped.setdefault(key, []).append(child)

    for parent, key in zip(parents, parent_keys):
        setattr(parent, relation_name, grouped.get(key, []))
