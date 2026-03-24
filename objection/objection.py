from enum import Enum
from typing import Dict, Type, Any, Union, TypedDict


class RelationType(Enum):
    """
    Defines the types of relations between models.
    """
    BelongsToOneRelation = "BelongsToOneRelation"
    HasManyRelation = "HasManyRelation"
    HasOneRelation = "HasOneRelation"
    ManyToManyRelation = "ManyToManyRelation"


BasicJoinMapping = TypedDict("BasicJoinMapping", {
    "from": str,
    "to": str,
})

ThroughJoinValue = TypedDict("ThroughJoinValue", {
    "table": Union[Type["Model"], str],
    "key": str,
})

ThroughJoinMapping = TypedDict("ThroughJoinMapping", {
    "from": ThroughJoinValue,
    "to": ThroughJoinValue,
})

JoinMappingWithThrough = TypedDict("JoinMappingWithThrough", {
    "from": str,
    "through": ThroughJoinMapping,
    "to": str,
})

Join = Union[BasicJoinMapping, JoinMappingWithThrough]


class RelationMapping(TypedDict):
    relation: RelationType
    modelClass: Union[Type["Model"], str]
    join: Join


class QueryBuilder:
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
        self._selected_columns = []
        self._joins = []
        self._where_clauses = []

    def select(self, *columns) -> "QueryBuilder":
        self._selected_columns.extend(columns)
        return self

    def __str__(self):
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

    def __getattr__(self, name: str):
        if name.endswith("JoinRelated"):
            join_prefix = name[:-len("JoinRelated")]

            if join_prefix not in self._JOIN_METHOD_MAP:
                valid_methods = [f"{prefix}JoinRelated" for prefix in self._JOIN_METHOD_MAP.keys()]
                raise AttributeError(f"'{name}' is not a valid join method. Valid methods are: {valid_methods}")

            sql_join_type = self._JOIN_METHOD_MAP[join_prefix]

            def dynamic_join_caller(relation_name: str, alias: str = None):
                self._join_related_internal(sql_join_type, relation_name, alias)
                return self

            return dynamic_join_caller

        import re
        match = re.match(r"^(or|and)?(Where|WhereIn|WhereNotIn)$", name, re.IGNORECASE)
        if match:
            conjunction_str, where_type_str = match.groups()

            if not self._where_clauses:
                if conjunction_str and conjunction_str.lower() != "and":
                     raise RuntimeError(f"Cannot start a where clause with '{conjunction_str}'.")
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
                        raise ValueError("Invalid arguments for 'where' method.")
                elif where_type == "wherein":
                    self._add_where_in_internal(conjunction, "IN", *args)
                elif where_type == "wherenotin":
                    self._add_where_in_internal(conjunction, "NOT IN", *args)
                return self
            return dynamic_where_caller

        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def _build_where_clause(self, column: str, operator: str, value: Any) -> str:
        formatted_value = value
        if isinstance(value, str):
            escaped_value = value.replace("'", "''")
            formatted_value = f"'{escaped_value}'"
        return f"{column} {operator} {formatted_value}"

    def _add_where_internal(self, conjunction: str, column_or_callable: Any, op: str = None, val: Any = None):
        if not self._where_clauses and conjunction:
            raise RuntimeError(f"Cannot use '{conjunction}' on the first where clause.")

        if callable(column_or_callable):
            temp_builder = QueryBuilder(self._model_class)
            column_or_callable(temp_builder)
            if temp_builder._where_clauses:
                grouped_clause_str = temp_builder._build_clause_list_string()
                self._where_clauses.append((conjunction, f"({grouped_clause_str})"))
        else:
            clause = self._build_where_clause(column_or_callable, op, val)
            self._where_clauses.append((conjunction, clause))

    def _add_where_in_internal(self, conjunction: str, op: str, col: str, vals: list):
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
        if not self._where_clauses:
            return ""

        parts = [self._where_clauses[0][1]]
        for conjunction, clause in self._where_clauses[1:]:
            parts.append(f"{conjunction} {clause}")
        return " ".join(parts)

    def _join_related_internal(self, join_type: str, relation_name: str, alias: str = None):
        relation = self._model_class.relationMappings.get(relation_name)
        if not relation:
            raise ValueError(f"Relation '{relation_name}' not found in model '{self._model_class.__name__}'")

        related_model_class = self._resolve_model_class(relation["modelClass"])
        join_info = relation["join"]

        if "through" in join_info:
            self._add_through_join(join_type, join_info, related_model_class, alias)
        else:
            self._add_basic_join(join_type, join_info, related_model_class, alias)

    def _resolve_model_class(self, model_class_ref: Union[Type["Model"], str]) -> Type["Model"]:
        if isinstance(model_class_ref, str):
            if model_class_ref in globals():
                return globals()[model_class_ref]
            else:
                raise ValueError(f"Could not resolve model class string '{model_class_ref}'")
        return model_class_ref

    def _add_basic_join(self, join_type: str, join_info: BasicJoinMapping, related_model_class: Type["Model"], alias: str = None):
        from_col = join_info["from"]
        to_col = join_info["to"]
        on_clause = f"{from_col} = {to_col}"

        final_related_table_name = related_model_class.tableName
        join_table_part = final_related_table_name
        if alias:
            join_table_part = f"{final_related_table_name} AS {alias}"
            to_table, to_column = to_col.split(".")
            if to_table == final_related_table_name:
                on_clause = f"{from_col} = {alias}.{to_column}"

        join_clause = f"{join_type} {join_table_part} ON {on_clause}"
        self._joins.append(join_clause)

    def _add_through_join(self, join_type: str, join_info: JoinMappingWithThrough, related_model_class: Type["Model"], alias: str = None):
        # First join (base to through)
        from_col = join_info["from"]
        through_from_mapping = join_info["through"]["from"]
        through_table = through_from_mapping["table"]
        if not isinstance(through_table, str):
            through_table = through_table.tableName
        through_from_key = through_from_mapping["key"]

        on_clause1 = f"{from_col} = {through_table}.{through_from_key}"
        join_clause1 = f"INNER JOIN {through_table} ON {on_clause1}"
        self._joins.append(join_clause1)

        # Second join (through to related)
        through_to_mapping = join_info["through"]["to"]
        through_to_key = through_to_mapping["key"]
        to_col = join_info["to"]

        related_table_name_from_model = related_model_class.tableName
        on_clause2 = f"{through_table}.{through_to_key} = {to_col}"

        join_table_part = related_table_name_from_model
        if alias:
            join_table_part = f"{related_table_name_from_model} AS {alias}"
            to_table, to_column = to_col.split(".")
            if to_table == related_table_name_from_model:
                on_clause2 = f"{through_table}.{through_to_key} = {alias}.{to_column}"

        join_clause2 = f"{join_type} {join_table_part} ON {on_clause2}"
        self._joins.append(join_clause2)


class Model:
    """
    A base model class that mimics the behavior of Objection.js models for
    defining relational mappings.

    To use this, create a subclass and define `tableName` and `relationMappings`
    """
    database: str = None
    tableName: str = None
    tableSchema: str = None
    relationMappings: Dict[str, RelationMapping] = {}

    def __init__(self, **kwargs):
        """
        Initializes a model instance, allowing attributes to be set from
        keyword arguments.
        """
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self):
        attributes = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"{self.__class__.__name__}({attributes})"

    def __getattr__(self, name: str) -> str:
        """
        Provides access to table columns as strings, following the format
        'database.tableSchema?.tableName.column'.
        """
        cls = self.__class__
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(f"'{cls.__name__}' object has no attribute '{name}'")

        database = getattr(cls, "database", None)
        table_schema = getattr(cls, "tableSchema", None)
        table_name = getattr(cls, "tableName", None)
        if database and table_name:
            if table_schema:
                return f"{database}.{table_schema}.{table_name}.{name}"
            return f"{database}.{table_name}.{name}"
        raise AttributeError(f"'{cls.__name__}' object has no attribute '{name}'")

    @classmethod
    def query(cls) -> "QueryBuilder":
        return QueryBuilder(cls)


def create_model(
    name: str,
    table_name: str,
    mappings: Dict[str, RelationMapping] = None,
    table_schema: str = None,
    database: str = None
) -> Type[Model]:
    """
    Dynamically creates a Model subclass.
    """
    if mappings is None:
        mappings = {}
    model_attrs = {'tableName': table_name, 'relationMappings': mappings}
    if table_schema:
        model_attrs['tableSchema'] = table_schema
    if database:
        model_attrs['database'] = database
    return type(name, (Model,), model_attrs)
