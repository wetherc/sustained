from __future__ import annotations

import re
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
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


class QueryBuilder:
    """
    A builder for creating and executing SQL queries in a programmatic way.

    This class is not meant to be instantiated directly. Instead, you should use
    the `query()` class method on a `Model` subclass.
    """

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
        """
        Initializes the QueryBuilder.

        Args:
            model_class (Type[Model]): The `Model` subclass this query is based on.
        """
        self._model_class = model_class
        self._selected_columns: List[str] = []
        self._joins: List[str] = []
        self._where_clauses: List[Tuple[str, str]] = []

    def select(self, *columns: str) -> "QueryBuilder":
        """
        Specifies the columns to be selected in the query.

        Args:
            *columns (str): A list of column names to select.

        Returns:
            QueryBuilder: The current QueryBuilder instance for chaining.
        """
        self._selected_columns.extend(columns)
        return self

    def __str__(self) -> str:
        """
        Builds and returns the final SQL query string.

        Returns:
            str: The complete SQL query.
        """
        cols = ", ".join(self._selected_columns) if self._selected_columns else "*"
        model_cls = self._model_class
        parts = []
        if model_cls.database:
            parts.append(model_cls.database)
        if model_cls.tableSchema:
            parts.append(model_cls.tableSchema)
        if model_cls.tableName:
            parts.append(model_cls.tableName)
        full_table_name = ".".join(parts)

        joins_str = " ".join(self._joins)

        where_str = ""
        if self._where_clauses:
            where_str = "WHERE " + self._build_clause_list_string()

        query_parts = [f"SELECT {cols} FROM {full_table_name}"]

        if joins_str:
            query_parts.append(joins_str)

        if where_str:
            query_parts.append(where_str)

        return " ".join(query_parts)

    def __getattr__(self, name: str) -> Callable:
        """
        Dynamically handles method calls for joins and where clauses.

        This allows for methods like `where('id', '=', 1)`, `orWhere(...)`,
        `innerJoinRelated('owner')`, etc., to be called on the query builder.

        Supported join methods:
            - `joinRelated`
            - `innerJoinRelated`
            - `leftJoinRelated`
            - `leftOuterJoinRelated`
            - `rightJoinRelated`
            - `rightOuterJoinRelated`
            - `fullJoinRelated`
            - `fullOuterJoinRelated`
            - `crossJoinRelated`

        Supported where methods:
            - `where`
            - `andWhere`
            - `orWhere`
            - `whereIn`
            - `andWhereIn`
            - `orWhereIn`
            - `whereNotIn`
            - `andWhereNotIn`
            - `orWhereNotIn`

        Args:
            name (str): The name of the method being called.

        Returns:
            Callable: A function that executes the corresponding join or where logic.

        Raises:
            AttributeError: If the method name is not a valid dynamic method.
        """
        # Handle join methods (e.g., innerJoinRelated, joinRelated)
        join_prefixes = "|".join(k for k in self._JOIN_METHOD_MAP.keys() if k)
        join_match = re.match(
            rf"^({join_prefixes})?(JoinRelated)$", name, re.IGNORECASE
        )

        if join_match:
            join_prefix, _ = join_match.groups()
            sql_join_type = self._JOIN_METHOD_MAP[join_prefix or ""]

            def dynamic_join_caller(relation_name: str, alias: Optional[str] = None):
                self._join_related_internal(sql_join_type, relation_name, alias)
                return self

            return dynamic_join_caller

        # Handle where methods (e.g., where, andWhereIn)
        where_match = re.match(
            r"^(or|and)?(Where|WhereIn|WhereNotIn)$", name, re.IGNORECASE
        )
        if where_match:
            conjunction_str, where_type_str = where_match.groups()

            if not self._where_clauses:
                if conjunction_str and conjunction_str.lower() != "and":
                    raise RuntimeError(
                        f"Cannot start a where clause with '{conjunction_str}'."
                    )
                conjunction = ""
            else:
                conjunction = (conjunction_str or "and").upper()

            where_type = where_type_str.lower()

            def dynamic_where_caller(*args):
                if where_type == "where":
                    if len(args) == 1 and callable(args[0]):
                        self._add_where_internal(conjunction, args[0])
                    elif len(args) == 3:
                        self._add_where_internal(conjunction, *args)
                    else:
                        raise ValueError(
                            "Invalid arguments for 'where' method. Use `where(column, operator, value)` or `where(lambda q: ...)`."
                        )
                elif where_type == "wherein":
                    self._add_where_in_internal(conjunction, "IN", *args)
                elif where_type == "wherenotin":
                    self._add_where_in_internal(conjunction, "NOT IN", *args)
                return self

            return dynamic_where_caller

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _build_where_clause(self, column: str, operator: str, value: Any) -> str:
        """Formats a single where clause condition."""
        formatted_value = value
        if isinstance(value, str):
            escaped_value = value.replace("'", "''")
            formatted_value = f"'{escaped_value}'"
        return f"{column} {operator} {formatted_value}"

    def _add_where_internal(
        self,
        conjunction: str,
        column_or_callable: Any,
        op: Optional[str] = None,
        val: Optional[Any] = None,
    ):
        """Internal handler for adding `where` clauses."""
        if not self._where_clauses and conjunction:
            raise RuntimeError(f"Cannot use '{conjunction}' on the first where clause.")

        if callable(column_or_callable):
            # Handle grouped where clauses, e.g., where(lambda q: q.where(...))
            temp_builder = QueryBuilder(self._model_class)
            column_or_callable(temp_builder)
            if temp_builder._where_clauses:
                grouped_clause_str = temp_builder._build_clause_list_string()
                self._where_clauses.append((conjunction, f"({grouped_clause_str})"))
        else:
            # We can assert op is not None because of the checks in __getattr__
            assert op is not None
            clause = self._build_where_clause(column_or_callable, op, val)
            self._where_clauses.append((conjunction, clause))

    def _add_where_in_internal(self, conjunction: str, op: str, col: str, vals: list):
        """Internal handler for adding `WHERE IN` and `WHERE NOT IN` clauses."""
        if not self._where_clauses and conjunction:
            raise RuntimeError(f"Cannot use '{conjunction}' on the first where clause.")

        formatted_values = []
        for v in vals:
            if isinstance(v, str):
                formatted_values.append(f"'{v.replace("'", "''")}'")
            else:
                formatted_values.append(str(v))
        values_str = ", ".join(formatted_values)
        clause = f"{col} {op} ({values_str})"
        self._where_clauses.append((conjunction, clause))

    def _build_clause_list_string(self) -> str:
        """Builds the complete WHERE clause string from all parts."""
        if not self._where_clauses:
            return ""

        # The first clause doesn't have a preceding conjunction
        parts = [self._where_clauses[0][1]]
        for conjunction, clause in self._where_clauses[1:]:
            parts.append(f"{conjunction} {clause}")
        return " ".join(parts)

    def _join_related_internal(
        self, join_type: str, relation_name: str, alias: Optional[str] = None
    ):
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
            basic_join_info = cast(BasicJoinMapping, join_info)
            self._add_basic_join(join_type, basic_join_info, related_model_class, alias)

    def _resolve_model_class(
        self, model_class_ref: Union[Type["Model"], str]
    ) -> Type["Model"]:
        """Resolves a model class reference (string or class) to a class type."""
        if isinstance(model_class_ref, str):
            # Try to find the model class in the global scope.
            # This is a simple mechanism for resolving string references.
            if model_class_ref in globals():
                return globals()[model_class_ref]
            else:
                raise ValueError(
                    f"Could not resolve model class string '{model_class_ref}'"
                )
        return model_class_ref

    def _add_basic_join(
        self,
        join_type: str,
        join_info: BasicJoinMapping,
        related_model_class: Type["Model"],
        alias: Optional[str] = None,
    ):
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
    ):
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
