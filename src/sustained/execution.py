"""
Execution support: running built queries against a DB-API 2.0 connection
and hydrating rows into model instances.

Bind a connection once with Model.bind(connection), or pass one to run()
per call. Any DB-API 2.0 connection works as long as its paramstyle matches
the dialect's placeholder: qmark for the default and MSSQL dialects
(sqlite3, pyodbc) and format for Postgres (psycopg, psycopg2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type

from sustained.types import RelationType

if TYPE_CHECKING:
    from sustained.model import Model


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
    if "through" in join_info:
        raise NotImplementedError(
            "Eager loading of through relations is not supported yet. "
            "Fetch the through rows with an explicit joinRelated query."
        )

    from sustained.model import resolve_model_reference

    related_cls = resolve_model_reference(
        relation["modelClass"], context_module=model_class.__module__
    )

    from_table, from_col = _split_column_ref(join_info["from"], relation_name)
    to_table, to_col = _split_column_ref(join_info["to"], relation_name)

    # The side whose table matches the parent model holds the parent key.
    if from_table == model_class.tableName:
        parent_col, child_col = from_col, to_col
    else:
        parent_col, child_col = to_col, from_col

    # Read hydrated values from __dict__ so a missing column is an error
    # instead of silently resolving to a qualified column string.
    parent_keys = []
    for parent in parents:
        if parent_col not in parent.__dict__:
            raise ValueError(
                f"Cannot eager load '{relation_name}': parent rows were not "
                f"fetched with the '{parent_col}' column."
            )
        parent_keys.append(parent.__dict__[parent_col])

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
