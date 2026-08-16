"""
Execution support: running built queries against a DB-API 2.0 connection
and hydrating rows into model instances.

Bind a connection once with Model.bind(connection), or pass one to run()
per call. Any DB-API 2.0 connection works as long as its paramstyle matches
the dialect's placeholder: qmark for the default and MSSQL dialects
(sqlite3, pyodbc) and format for Postgres (psycopg, psycopg2).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
)

from sustained.types import (
    Binding,
    Connection,
    Cursor,
    JoinMappingWithThrough,
    RelationTree,
    RelationType,
    RowValue,
    SqlValue,
)

if TYPE_CHECKING:
    from sustained.model import Model
    from sustained.types import AnyQuery


# Connections with an open transaction, keyed by id(). The value holds a
# strong reference to the connection, so the id cannot be reused while the
# entry exists, plus the current savepoint nesting depth.
_ACTIVE_TRANSACTIONS: Dict[int, Tuple[Connection, int]] = {}

# Optional observer called after every executed statement.
_statement_listener: Optional[Callable[[str, Tuple[SqlValue, ...], float], None]] = None


def set_statement_listener(
    listener: Optional[Callable[[str, Tuple[SqlValue, ...], float], None]],
) -> None:
    """
    Registers a callable invoked after every statement run() executes, with
    the SQL text, the parameter tuple, and the duration in seconds. Pass
    None to remove the listener. Useful for logging and timing.
    """
    global _statement_listener
    _statement_listener = listener


def notify_statement(sql: str, params: Tuple[SqlValue, ...], duration: float) -> None:
    """Invokes the registered statement listener, if any."""
    if _statement_listener is not None:
        _statement_listener(sql, params, duration)


# Per-thread stack of connections pinned by transaction() blocks that were
# opened against a pool. Statements inside the block use the pinned
# connection instead of checking a fresh one out.
_thread_state = threading.local()


def _pinned_connection() -> Optional[Connection]:
    stack = getattr(_thread_state, "pinned", None)
    return stack[-1] if stack else None


def _pin(connection: Connection) -> None:
    stack = getattr(_thread_state, "pinned", None)
    if stack is None:
        stack = []
        _thread_state.pinned = stack
    stack.append(connection)


def _unpin() -> None:
    _thread_state.pinned.pop()


@contextmanager
def connection_scope(
    explicit: Optional[Binding], binding: Optional[Binding]
) -> Iterator[Connection]:
    """
    Resolves the connection for one statement. An explicit argument wins;
    then a connection pinned by an open transaction() block on this thread;
    then the model binding. Pools check a connection out for the scope.
    """
    from sustained.pool import ConnectionPool

    if explicit is not None:
        if isinstance(explicit, ConnectionPool):
            with explicit.connection() as conn:
                yield conn
        else:
            yield explicit
        return

    pinned = _pinned_connection()
    if pinned is not None:
        yield pinned
        return

    if binding is None:
        raise RuntimeError(
            "No database connection. Bind one with Model.bind(connection) "
            "or pass it to run()."
        )
    if isinstance(binding, ConnectionPool):
        with binding.connection() as conn:
            yield conn
    else:
        yield binding


def in_transaction(connection: Connection) -> bool:
    """Reports whether the connection has an open transaction() context."""
    entry = _ACTIVE_TRANSACTIONS.get(id(connection))
    return entry is not None and entry[0] is connection


@contextmanager
def transaction(connection: Binding) -> Iterator[Connection]:
    """
    Runs the block atomically on the connection. Commits when the block
    finishes and rolls back when it raises. While the context is open,
    run() stops committing per statement.

    Nested contexts on the same connection use ANSI savepoints, so an inner
    failure rolls back only the inner block.

    When given a ConnectionPool, one connection is checked out, pinned to
    the calling thread for the duration of the block, and released after.
    """
    from sustained.pool import ConnectionPool

    if isinstance(connection, ConnectionPool):
        pinned = _pinned_connection()
        if pinned is not None:
            # A transaction is already open on this thread; nest on its
            # connection with a savepoint instead of checking out another.
            with transaction(pinned):
                yield pinned
            return
        conn = connection.acquire_raw()
        _pin(conn)
        try:
            with transaction(conn):
                yield conn
        finally:
            _unpin()
            connection.release(conn)
        return

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


def fetch_models(model_class: Type["Model"], cursor: Cursor) -> List["Model"]:
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


def related_model(model_class: Type["Model"], relation_name: str) -> Type["Model"]:
    """
    Returns the model class on the far side of a relation.

    Raises:
        ValueError: If the model has no relation with that name.
    """
    relation = model_class.relationMappings.get(relation_name)
    if not relation:
        raise ValueError(
            f"Relation '{relation_name}' not found in model '{model_class.__name__}'"
        )
    from sustained.model import resolve_model_reference

    return resolve_model_reference(
        relation["modelClass"], context_module=model_class.__module__
    )


def check_relation_path(model_class: Type["Model"], path: str) -> None:
    """
    Walks a dotted relation path and rejects the first unknown segment.

    Raises:
        ValueError: Naming the segment, the model that lacks it, and the
            full path when the path has more than one segment.
    """
    current = model_class
    for segment in path.split("."):
        try:
            current = related_model(current, segment)
        except ValueError as exc:
            if "." in path:
                raise ValueError(f"{exc} (in relation path '{path}')") from None
            raise


def relation_tree(paths: List[str]) -> RelationTree:
    """
    Folds dotted relation paths into a nested dict, so paths sharing a
    prefix load that prefix once.
    """
    tree: RelationTree = {}
    for path in paths:
        node = tree
        for segment in path.split("."):
            node = node.setdefault(segment, {})
    return tree


def _attached_children(parents: List["Model"], relation_name: str) -> List["Model"]:
    """Flattens what an eager load attached, ready to be the next parents."""
    children: List["Model"] = []
    for parent in parents:
        loaded = getattr(parent, relation_name, None)
        if isinstance(loaded, list):
            children.extend(loaded)
        elif loaded is not None:
            children.append(loaded)
    return children


def eager_load_paths(
    model_class: Type["Model"],
    connection: Connection,
    parents: List["Model"],
    paths: List[str],
) -> None:
    """
    Loads every dotted relation path for a list of parent instances. Each
    relation costs one query per level, batched over all the parents at
    that level.
    """
    _eager_load_tree(model_class, connection, parents, relation_tree(paths))


def _eager_load_tree(
    model_class: Type["Model"],
    connection: Connection,
    parents: List["Model"],
    tree: RelationTree,
) -> None:
    """Loads one level of the relation tree, then recurses into each child."""
    for relation_name, children in tree.items():
        eager_load_relation(model_class, connection, parents, relation_name)
        if not children:
            continue
        next_parents = _attached_children(parents, relation_name)
        if next_parents:
            _eager_load_tree(
                related_model(model_class, relation_name),
                connection,
                next_parents,
                children,
            )


class EagerPlan:
    """
    One eager load, split into the query to run and how to attach its rows.
    The split lets the sync and async paths share the SQL and the grouping,
    since only the way they run the query differs.

    A None query means there is nothing to fetch, and attaching sets the
    empty value on every parent.
    """

    def __init__(
        self,
        relation_name: str,
        parent_keys: List[RowValue],
        is_many: bool,
        query: Optional["AnyQuery"] = None,
        child_col: Optional[str] = None,
        through: bool = False,
    ) -> None:
        self.relation_name = relation_name
        self.parent_keys = parent_keys
        self.is_many = is_many
        self.query = query
        self.child_col = child_col
        self.through = through


def plan_eager_load(
    model_class: Type["Model"],
    parents: List["Model"],
    relation_name: str,
) -> EagerPlan:
    """
    Builds the query that loads a relation for a list of parents, batched
    over their join keys.

    Raises:
        ValueError: If the model has no relation with that name, or the
            parent rows lack the join key column.
    """
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
        # The key test above is what tells the two join mappings apart; a
        # type checker cannot read it, so the narrowing is spelled out.
        through_join = cast(JoinMappingWithThrough, join_info)
        return _plan_eager_load_through(
            related_cls, parents, relation_name, through_join, from_col, to_col
        )

    # The side whose table matches the parent model holds the parent key.
    if from_table == model_class.tableName:
        parent_col, child_col = from_col, to_col
    else:
        parent_col, child_col = to_col, from_col

    parent_keys = _collect_parent_keys(parents, parent_col, relation_name)
    unique_keys = [k for k in dict.fromkeys(parent_keys) if k is not None]
    is_many = relation["relation"] == RelationType.HasManyRelation
    if not unique_keys:
        return EagerPlan(relation_name, parent_keys, is_many)
    return EagerPlan(
        relation_name,
        parent_keys,
        is_many,
        query=related_cls.query().whereIn(child_col, unique_keys),
        child_col=child_col,
    )


def attach_eager_load(
    plan: EagerPlan, parents: List["Model"], children: List["Model"]
) -> None:
    """
    Groups fetched rows by their join key and attaches them to the parents
    under the relation name. HasManyRelation and ManyToManyRelation attach
    a list; the to-one types attach a single instance or None.

    Raises:
        ValueError: If the fetched rows lack the join key column.
    """
    empty: Optional[List["Model"]] = [] if plan.is_many else None
    if plan.query is None:
        for parent in parents:
            setattr(parent, plan.relation_name, [] if plan.is_many else None)
        return

    grouped: Dict[RowValue, List["Model"]] = {}
    for child in children:
        if plan.through:
            key = child.__dict__.pop(_PARENT_KEY_ALIAS, None)
        else:
            assert plan.child_col is not None
            if plan.child_col not in child.__dict__:
                raise ValueError(
                    f"Cannot eager load '{plan.relation_name}': related rows "
                    f"do not include the '{plan.child_col}' column."
                )
            key = child.__dict__[plan.child_col]
        grouped.setdefault(key, []).append(child)

    for parent, key in zip(parents, plan.parent_keys):
        matches = grouped.get(key, [])
        if plan.is_many:
            setattr(parent, plan.relation_name, matches)
        else:
            setattr(parent, plan.relation_name, matches[0] if matches else empty)


def eager_load_relation(
    model_class: Type["Model"],
    connection: Connection,
    parents: List["Model"],
    relation_name: str,
) -> None:
    """
    Loads a relation for a list of parent instances with one extra query and
    attaches the results to each parent under the relation name.

    HasManyRelation and ManyToManyRelation attach a list; the to-one relation
    types attach a single instance or None.
    """
    if not parents:
        return
    plan = plan_eager_load(model_class, parents, relation_name)
    children = plan.query.run(connection) if plan.query is not None else []
    attach_eager_load(plan, parents, children)


def _collect_parent_keys(
    parents: List["Model"], parent_col: str, relation_name: str
) -> List[RowValue]:
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


def _plan_eager_load_through(
    related_cls: Type["Model"],
    parents: List["Model"],
    relation_name: str,
    join_info: JoinMappingWithThrough,
    parent_col: str,
    related_col: str,
) -> EagerPlan:
    """
    Plans a many-to-many load as one query that joins the related table to
    the through table and exposes the parent key under a reserved alias for
    grouping.
    """
    through = join_info["through"]

    def through_table_name(ref: Union[Type["Model"], str]) -> str:
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
        return EagerPlan(relation_name, parent_keys, is_many=True)

    query = (
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
    )
    return EagerPlan(
        relation_name, parent_keys, is_many=True, query=query, through=True
    )
