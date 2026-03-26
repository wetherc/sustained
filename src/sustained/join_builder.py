from __future__ import annotations

import re
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    List,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
)

from .types import BasicJoinMapping, JoinMappingWithThrough

if TYPE_CHECKING:
    from .model import Model


class OnClauseBuilder:
    """
    A helper class for building complex JOIN ... ON clauses.
    An instance of this is passed to the lambda in `...join(..., lambda j: ...)` calls.
    """

    def __init__(self) -> None:
        self._conditions: List[Tuple[str, str]] = []

    def on(self, col1: str, op: str, col2: str) -> "OnClauseBuilder":
        """Adds an ON condition. If this is not the first condition, it's treated as AND ON."""
        conjunction = "AND" if self._conditions else ""
        self._add_condition(conjunction, col1, op, col2)
        return self

    def andOn(self, col1: str, op: str, col2: str) -> "OnClauseBuilder":
        """Adds an AND ON condition."""
        if not self._conditions:
            raise RuntimeError(
                "Cannot use 'andOn' for the first join condition. Use 'on' instead."
            )
        self._add_condition("AND", col1, op, col2)
        return self

    def orOn(self, col1: str, op: str, col2: str) -> "OnClauseBuilder":
        """Adds an OR ON condition."""
        if not self._conditions:
            raise RuntimeError(
                "Cannot use 'orOn' for the first join condition. Use 'on' instead."
            )
        self._add_condition("OR", col1, op, col2)
        return self

    def _add_condition(self, conjunction: str, col1: str, op: str, col2: str) -> None:
        condition_str = f"{col1} {op} {col2}"
        self._conditions.append((conjunction, condition_str))

    def __str__(self) -> str:
        """Builds the final ON clause string."""
        if not self._conditions:
            raise RuntimeError("A join condition must be specified inside the lambda.")

        # The first condition doesn't have a preceding conjunction
        parts = [self._conditions[0][1]]
        for conjunction, clause in self._conditions[1:]:
            parts.append(f"{conjunction} {clause}")
        return " ".join(parts)


class JoinClauseBuilder:
    """A helper class for building JOIN clauses."""

    _JOIN_METHOD_MAP = {
        "": "JOIN",
        "inner": "INNER JOIN",
        "left": "LEFT JOIN",
        "leftOuter": "LEFT OUTER JOIN",
        "right": "RIGHT JOIN",
        "rightOuter": "RIGHT OUTER JOIN",
        "full": "FULL JOIN",
        "fullOuter": "FULL OUTER JOIN",
        "cross": "CROSS JOIN",
    }

    def __init__(self, model_class: Type["Model"]):
        self._model_class = model_class
        self._joins: List[str] = []

    def __str__(self) -> str:
        return " ".join(self._joins)

    def __getattr__(self, name: str) -> Callable[..., "JoinClauseBuilder"]:
        """
        Dynamically handles method calls for joins.
        """
        join_prefixes = "|".join(k for k in self._JOIN_METHOD_MAP.keys() if k)
        join_match = re.match(
            rf"^({join_prefixes})?(Join)(Related)?$", name, re.IGNORECASE
        )

        if join_match:
            join_prefix, _, related_suffix = join_match.groups()
            sql_join_type = self._JOIN_METHOD_MAP[join_prefix or ""]

            if related_suffix:
                # This is a ...joinRelated() call
                def dynamic_join_caller(
                    relation_name: str, alias: Optional[str] = None
                ) -> "JoinClauseBuilder":
                    self._join_related_internal(sql_join_type, relation_name, alias)
                    return self

                return dynamic_join_caller
            else:
                # This is a raw ...join() call
                def dynamic_raw_join_caller(
                    table: str, *args: Any
                ) -> "JoinClauseBuilder":
                    if len(args) == 3:
                        # Static syntax: .join('table', 'col1', '=', 'col2')
                        col1, op, col2 = args
                        on_clause = f"{col1} {op} {col2}"
                    elif len(args) == 1 and callable(args[0]):
                        # Composable syntax: .join('table', lambda j: ...)
                        join_builder_fn = args[0]
                        on_clause_builder = OnClauseBuilder()
                        join_builder_fn(on_clause_builder)
                        on_clause = str(on_clause_builder)
                    else:
                        raise ValueError(
                            "Invalid arguments for join method. Use `join(table, col1, op, col2)` or `join(table, lambda j: ...)`."
                        )

                    join_clause = f"{sql_join_type} {table} ON {on_clause}"
                    self._joins.append(join_clause)
                    return self

                return dynamic_raw_join_caller

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _join_related_internal(
        self, join_type: str, relation_name: str, alias: Optional[str] = None
    ) -> None:
        """Internal handler for adding a join based on a defined relation."""
        relation = self._model_class.relationMappings.get(relation_name)
        if not relation:
            raise ValueError(
                f"Relation '{relation_name}' not found in model '{self._model_class.__name__}'"
            )

        related_model_class = self._resolve_model_class(relation["modelClass"])
        join_info = relation["join"]

        if "through" in join_info:
            # Cast to the more specific TypedDict to satisfy mypy
            through_join_info = cast(JoinMappingWithThrough, join_info)
            self._add_through_join(
                join_type, through_join_info, related_model_class, alias
            )
        else:
            basic_join_info = join_info
            self._add_basic_join(join_type, basic_join_info, related_model_class, alias)

    def _resolve_model_class(
        self, model_class_ref: Union[Type["Model"], str]
    ) -> Type["Model"]:
        """Resolves a model class reference (string or class) to a class type."""
        if isinstance(model_class_ref, str):
            # This is a simple mechanism for resolving string references. It may not be robust
            # enough for all use cases, e.g. circular dependencies between modules.
            # A better implementation might involve a centralized model registry.
            module = self._model_class.__module__
            models = __import__(module, fromlist=[model_class_ref])
            return cast(Type["Model"], getattr(models, model_class_ref))
        return model_class_ref

    def _add_basic_join(
        self,
        join_type: str,
        join_info: BasicJoinMapping,
        related_model_class: Type["Model"],
        alias: Optional[str] = None,
    ) -> None:
        """Adds a basic (e.g., one-to-one, one-to-many) join to the query."""
        final_related_table_name = related_model_class.tableName
        assert (
            final_related_table_name is not None
        ), "Model used in a relation must have a tableName"

        from_col = join_info["from"]
        to_col = join_info["to"]
        on_clause = f"{from_col} = {to_col}"

        join_table_part = final_related_table_name
        if alias:
            join_table_part = f"{final_related_table_name} AS {alias}"
            # If an alias is used, update the `ON` clause to reference it.
            to_table, to_column = to_col.split(".")
            if to_table == final_related_table_name:
                on_clause = f"{from_col} = {alias}.{to_column}"

        join_clause = f"{join_type} {join_table_part} ON {on_clause}"
        self._joins.append(join_clause)

    def _add_through_join(
        self,
        join_type: str,
        join_info: JoinMappingWithThrough,
        related_model_class: Type["Model"],
        alias: Optional[str] = None,
    ) -> None:
        """Adds a many-to-many join using a 'through' table."""
        related_table_name_from_model = related_model_class.tableName
        assert (
            related_table_name_from_model is not None
        ), "Model used in a relation must have a tableName"

        # First join: from the base model's table to the 'through' table.
        from_col = join_info["from"]
        through_from_mapping = join_info["through"]["from"]

        through_table_ref = through_from_mapping["table"]
        through_table_name: str
        if isinstance(through_table_ref, str):
            through_table_name = through_table_ref
        else:
            table_name = through_table_ref.tableName
            assert (
                table_name is not None
            ), "Model used as a through table must have a tableName"
            through_table_name = table_name

        through_from_key = through_from_mapping["key"]

        on_clause1 = f"{from_col} = {through_table_name}.{through_from_key}"
        # The join to the through table is always an INNER JOIN.
        join_clause1 = f"INNER JOIN {through_table_name} ON {on_clause1}"
        self._joins.append(join_clause1)

        # Second join: from the 'through' table to the final related model's table.
        through_to_mapping = join_info["through"]["to"]
        through_to_key = through_to_mapping["key"]
        to_col = join_info["to"]

        on_clause2 = f"{through_table_name}.{through_to_key} = {to_col}"

        join_table_part = related_table_name_from_model
        if alias:
            join_table_part = f"{related_table_name_from_model} AS {alias}"
            # If an alias is used, update the `ON` clause to reference it.
            to_table, to_column = to_col.split(".")
            if to_table == related_table_name_from_model:
                on_clause2 = (
                    f"{through_table_name}.{through_to_key} = {alias}.{to_column}"
                )

        join_clause2 = f"{join_type} {join_table_part} ON {on_clause2}"
        self._joins.append(join_clause2)
