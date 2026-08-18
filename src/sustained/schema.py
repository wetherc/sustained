"""
Typed column definitions and model-driven DDL.

Models declare their physical shape with tableColumns, a dict of column
name to ColumnDef. The dialect compiler maps each logical type to the
engine's SQL type, so one declaration generates CREATE TABLE for every
supported dialect.
"""

from __future__ import annotations

import enum as _pyenum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple, Union

from sustained.types import Expression, SqlValue

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler


class ColumnDef:
    """
    Declares one column of a table.

    Attributes:
        type_name: The logical type, e.g. 'INTEGER' or 'VARCHAR'.
        length: Length for VARCHAR types.
        precision, scale: Precision and scale for NUMERIC types.
        primary_key: Part of the primary key. Several columns may set this
            to form a composite key.
        nullable: Whether NULL is allowed. Primary key columns are always
            NOT NULL.
        unique: Adds a UNIQUE constraint on the column.
        default: A literal default value, or an Expression for raw SQL such
            as Expression('CURRENT_TIMESTAMP').
        references: A 'table.column' string rendered as a foreign key.
        autoincrement: Generates identity values. Only valid on an integer
            primary key column.
        backfill: A value or Expression used to fill existing rows when
            migration autogeneration adds this column as NOT NULL or
            tightens it to NOT NULL.
        enum_name: The name of the enum type, for ENUM columns. Postgres
            creates a type object with this name.
        enum_values: The permitted values of an ENUM column, in order.
    """

    def __init__(
        self,
        type_name: str,
        *,
        length: Optional[int] = None,
        precision: Optional[int] = None,
        scale: Optional[int] = None,
        primary_key: bool = False,
        nullable: bool = True,
        unique: bool = False,
        default: SqlValue = None,
        references: Optional[str] = None,
        autoincrement: bool = False,
        backfill: SqlValue = None,
        enum_name: Optional[str] = None,
        enum_values: Optional[Sequence[str]] = None,
    ) -> None:
        if autoincrement and type_name not in ("INTEGER", "BIGINT"):
            raise ValueError("autoincrement requires an Integer or BigInteger column.")
        if autoincrement and not primary_key:
            raise ValueError("autoincrement requires primary_key=True.")
        if references is not None and "." not in references:
            raise ValueError("references must be a 'table.column' string.")
        if type_name == "ENUM":
            enum_values = _checked_enum_values(enum_name, enum_values)
            if (
                default is not None
                and not isinstance(default, Expression)
                and default not in enum_values
            ):
                raise ValueError(
                    f"Default {default!r} is not a value of enum "
                    f"'{enum_name}'. Values: {', '.join(enum_values)}."
                )
        elif enum_name is not None or enum_values is not None:
            raise ValueError(
                "enum_name and enum_values are only valid on an ENUM column."
            )
        self.type_name = type_name
        self.length = length
        self.precision = precision
        self.scale = scale
        self.primary_key = primary_key
        self.nullable = nullable and not primary_key
        self.unique = unique
        self.default = default
        self.references = references
        self.autoincrement = autoincrement
        self.backfill = backfill
        self.enum_name = enum_name
        self.enum_values: Optional[Tuple[str, ...]] = (
            tuple(enum_values) if enum_values is not None else None
        )


def _checked_enum_values(
    enum_name: Optional[str], enum_values: Optional[Sequence[str]]
) -> Tuple[str, ...]:
    """Validates the name and value list of an ENUM column."""
    if not enum_name:
        raise ValueError(
            "An enum column needs a name. Pass name='...'; the name "
            "identifies the type in the database and in diffs."
        )
    if not enum_values:
        raise ValueError(f"Enum '{enum_name}' needs at least one value.")
    values = tuple(enum_values)
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Enum '{enum_name}' values must be non-empty strings; "
                f"got {value!r}."
            )
    if len(set(values)) != len(values):
        raise ValueError(f"Enum '{enum_name}' has duplicate values.")
    return values


class TableOptions:
    """
    Storage clauses that follow the column list of CREATE TABLE, for
    engines that need them. Athena renders these as PARTITIONED BY,
    LOCATION, and TBLPROPERTIES; every other dialect raises when any
    option is set.

    Attributes:
        location: The storage location, e.g. an s3:// path.
        partitioned_by: Partition columns or transforms, rendered as
            given, so Iceberg transforms like day(created_at) work.
        properties: Table properties, e.g. {'table_type': 'ICEBERG'}.
    """

    def __init__(
        self,
        location: Optional[str] = None,
        partitioned_by: Optional[List[str]] = None,
        properties: Optional[Dict[str, str]] = None,
    ) -> None:
        self.location = location
        self.partitioned_by = list(partitioned_by) if partitioned_by else []
        self.properties = dict(properties) if properties else {}


# The referential actions SQL defines for ON DELETE and ON UPDATE.
FOREIGN_KEY_ACTIONS = ("CASCADE", "SET NULL", "RESTRICT", "NO ACTION", "SET DEFAULT")


class Check:
    """
    A named CHECK constraint on a table. List instances in the model's
    `tableConstraints` attribute.

    The expression is SQL and renders as written, so column names inside
    it follow the dialect's quoting only if you quote them yourself. The
    name is required: diffs and down steps address a constraint by name.

    Attributes:
        name: The constraint name; must be unique within the table.
        expression: The SQL expression each row must satisfy.
    """

    def __init__(self, name: str, expression: str) -> None:
        if not name:
            raise ValueError("A check constraint needs a name.")
        if not expression or not expression.strip():
            raise ValueError(f"Check '{name}' needs a SQL expression.")
        self.name = name
        self.expression = expression.strip()


class ForeignKey:
    """
    A named foreign key constraint on a table. List instances in the
    model's `tableConstraints` attribute. For a single column with no
    actions, ColumnDef's `references` shorthand does the same thing.

    Attributes:
        name: The constraint name; must be unique within the table.
        columns: The constrained columns, in order.
        references: The target, as 'table.column' strings, one per
            constrained column. Every target column must belong to the
            same table.
        on_delete, on_update: Referential actions. One of CASCADE,
            SET NULL, RESTRICT, NO ACTION, SET DEFAULT.
    """

    def __init__(
        self,
        name: str,
        columns: Union[str, Sequence[str]],
        references: Union[str, Sequence[str]],
        on_delete: Optional[str] = None,
        on_update: Optional[str] = None,
    ) -> None:
        if not name:
            raise ValueError("A foreign key needs a name.")
        self.name = name
        self.columns = (columns,) if isinstance(columns, str) else tuple(columns)
        targets = (references,) if isinstance(references, str) else tuple(references)
        if not self.columns:
            raise ValueError(f"Foreign key '{name}' needs at least one column.")
        if len(targets) != len(self.columns):
            raise ValueError(
                f"Foreign key '{name}' constrains {len(self.columns)} "
                f"column(s) but references {len(targets)} target column(s)."
            )
        tables = []
        target_columns = []
        for target in targets:
            if "." not in target:
                raise ValueError(
                    f"Foreign key '{name}' references must be "
                    f"'table.column' strings; got {target!r}."
                )
            table, column = target.rsplit(".", 1)
            tables.append(table)
            target_columns.append(column)
        if len(set(tables)) != 1:
            raise ValueError(
                f"Foreign key '{name}' references more than one table: "
                f"{', '.join(sorted(set(tables)))}. One constraint targets "
                "one table."
            )
        self.target_table = tables[0]
        self.target_columns = tuple(target_columns)
        self.on_delete = _checked_fk_action(name, "on_delete", on_delete)
        self.on_update = _checked_fk_action(name, "on_update", on_update)


TableConstraint = Union[Check, ForeignKey]


def _checked_fk_action(
    fk_name: str, parameter: str, action: Optional[str]
) -> Optional[str]:
    """Validates and normalizes a referential action."""
    if action is None:
        return None
    normalized = " ".join(action.upper().split())
    if normalized not in FOREIGN_KEY_ACTIONS:
        raise ValueError(
            f"Foreign key '{fk_name}' {parameter}={action!r} is not a "
            f"referential action. Use one of: "
            f"{', '.join(FOREIGN_KEY_ACTIONS)}."
        )
    return normalized


class Index:
    """
    Declares a named index on a model. List instances in the model's
    `indexes` attribute.

    Attributes:
        name: The index name; must be unique within the database.
        columns: The indexed columns, in order.
        unique: Whether the index enforces uniqueness.
    """

    def __init__(self, name: str, *columns: str, unique: bool = False) -> None:
        if not name:
            raise ValueError("An index needs a name.")
        if not columns:
            raise ValueError(f"Index '{name}' needs at least one column.")
        self.name = name
        self.columns = tuple(columns)
        self.unique = unique


# Each factory forwards its keyword arguments to ColumnDef, which checks
# them. Repeating the full parameter list ten times is what it would take
# to name them here: PEP 692 typed **kwargs needs Python 3.11, and this
# package supports 3.9.
def Integer(**kwargs: Any) -> ColumnDef:
    """A 32-bit integer column."""
    return ColumnDef("INTEGER", **kwargs)


def BigInteger(**kwargs: Any) -> ColumnDef:
    """A 64-bit integer column."""
    return ColumnDef("BIGINT", **kwargs)


def String(length: int = 255, **kwargs: Any) -> ColumnDef:
    """A variable-length string column with a length limit."""
    return ColumnDef("VARCHAR", length=length, **kwargs)


def Text(**kwargs: Any) -> ColumnDef:
    """An unbounded text column."""
    return ColumnDef("TEXT", **kwargs)


def Boolean(**kwargs: Any) -> ColumnDef:
    """A boolean column. Renders as BIT on MSSQL."""
    return ColumnDef("BOOLEAN", **kwargs)


def Float(**kwargs: Any) -> ColumnDef:
    """A double-precision floating point column."""
    return ColumnDef("FLOAT", **kwargs)


def Numeric(precision: int = 18, scale: int = 6, **kwargs: Any) -> ColumnDef:
    """An exact decimal column."""
    return ColumnDef("NUMERIC", precision=precision, scale=scale, **kwargs)


def Date(**kwargs: Any) -> ColumnDef:
    """A calendar date column."""
    return ColumnDef("DATE", **kwargs)


def Timestamp(**kwargs: Any) -> ColumnDef:
    """A date and time column. Renders as DATETIME2 on MSSQL."""
    return ColumnDef("TIMESTAMP", **kwargs)


def Json(**kwargs: Any) -> ColumnDef:
    """A JSON document column. Renders as JSONB on Postgres."""
    return ColumnDef("JSON", **kwargs)


def Enum(
    *values: Union[str, type],
    name: str,
    **kwargs: Any,
) -> ColumnDef:
    """
    An enumerated string column: a named, ordered list of permitted
    values. Pass the values as strings, or pass one Python enum.Enum
    class whose member values are strings.

    `name` is required. A Postgres enum is a database object with its own
    identity; an explicit name gives stable diffs and lets two models
    share one type. The same name with the same values in two models is
    one type; the same name with different values is an error.

    Rendering follows the dialect: Postgres and DuckDB create a named
    type, MySQL renders an inline ENUM(...), and ANSI, SQLite, and MSSQL
    render VARCHAR with a named CHECK constraint. Presto and Athena
    refuse the column. Hydrated values stay plain strings.
    """
    if (
        len(values) == 1
        and isinstance(values[0], type)
        and issubclass(values[0], _pyenum.Enum)
    ):
        members = [member.value for member in values[0]]
        for member in members:
            if not isinstance(member, str):
                raise ValueError(
                    f"Enum '{name}' takes an enum class with string "
                    f"values; member value {member!r} is not a string."
                )
        value_list: List[str] = members
    else:
        value_list = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError(
                    f"Enum '{name}' values must be strings or one "
                    f"enum.Enum class; got {value!r}."
                )
            value_list.append(value)
    return ColumnDef("ENUM", enum_name=name, enum_values=value_list, **kwargs)


def collect_enum_types(columns: Dict[str, ColumnDef]) -> Dict[str, Tuple[str, ...]]:
    """
    The enum types the columns use, name to value tuple. Two columns may
    share a type when their values match; the same name with different
    values raises.
    """
    types: Dict[str, Tuple[str, ...]] = {}
    for column_name, col in columns.items():
        if col.type_name != "ENUM":
            continue
        assert col.enum_name is not None and col.enum_values is not None
        known = types.get(col.enum_name)
        if known is not None and known != col.enum_values:
            raise ValueError(
                f"Enum '{col.enum_name}' is declared twice with different "
                f"values: {', '.join(known)} versus "
                f"{', '.join(col.enum_values)}."
            )
        types[col.enum_name] = col.enum_values
    return types


def bare_table_name(table_sql: str) -> str:
    """
    The unquoted table name at the end of a rendered table reference,
    for building constraint names.
    """
    last = table_sql.rsplit(".", 1)[-1]
    return last.strip('"`[]')


def enum_check_constraint_sql(
    compiler: "Compiler", table_name: str, column_name: str, col: ColumnDef
) -> str:
    """
    Renders the named CHECK constraint that holds an enum column to its
    values, on dialects without an enum type.
    """
    assert col.enum_values is not None
    constraint = compiler.quote_identifier(f"ck_{table_name}_{column_name}_enum")
    column_sql = compiler.quote_identifier(column_name)
    values_sql = ", ".join(compiler.format_value(v) for v in col.enum_values)
    return f"CONSTRAINT {constraint} CHECK ({column_sql} IN ({values_sql}))"


def check_constraint_sql(compiler: "Compiler", check: Check) -> str:
    """Renders a named CHECK constraint for a table body or ADD."""
    constraint = compiler.quote_identifier(check.name)
    return f"CONSTRAINT {constraint} CHECK ({check.expression})"


def foreign_key_constraint_sql(compiler: "Compiler", fk: ForeignKey) -> str:
    """Renders a named FOREIGN KEY constraint for a table body or ADD."""
    constraint = compiler.quote_identifier(fk.name)
    columns_sql = ", ".join(compiler.quote_identifier(c) for c in fk.columns)
    target_table = compiler.quote_fully_qualified_identifier(fk.target_table)
    target_sql = ", ".join(compiler.quote_identifier(c) for c in fk.target_columns)
    sql = (
        f"CONSTRAINT {constraint} FOREIGN KEY ({columns_sql}) "
        f"REFERENCES {target_table} ({target_sql})"
    )
    if fk.on_delete is not None:
        sql += f" ON DELETE {fk.on_delete}"
    if fk.on_update is not None:
        sql += f" ON UPDATE {fk.on_update}"
    return sql


def table_constraint_sql(compiler: "Compiler", constraint: TableConstraint) -> str:
    """Renders one declared table constraint, checking dialect support."""
    if not compiler.supports_constraints():
        from sustained.exceptions import DialectError

        kind = "check" if isinstance(constraint, Check) else "foreign key"
        raise DialectError(
            f"The '{compiler.dialect_name()}' dialect enforces no table "
            f"constraints, so the {kind} constraint "
            f"'{constraint.name}' cannot be declared. Remove it from "
            "tableConstraints and validate rows in the application."
        )
    if isinstance(constraint, Check):
        return check_constraint_sql(compiler, constraint)
    return foreign_key_constraint_sql(compiler, constraint)


def render_column_sql(
    compiler: "Compiler",
    name: str,
    col: ColumnDef,
    inline_pk: bool,
) -> str:
    """Renders one column definition for CREATE TABLE or ADD COLUMN."""
    compiler.validate_column_def(col)
    parts = [compiler.quote_identifier(name), compiler.compile_column_type(col)]
    if col.autoincrement:
        identity = compiler.compile_identity()
        if identity:
            parts.append(identity)
    if col.primary_key and inline_pk:
        parts.append("PRIMARY KEY")
    elif not col.nullable:
        parts.append("NOT NULL")
    if col.unique and not col.primary_key:
        parts.append("UNIQUE")
    if col.default is not None:
        parts.append(f"DEFAULT {compiler.format_value(col.default)}")
    if col.references is not None and compiler.inline_references():
        parts.append(f"REFERENCES {reference_target_sql(compiler, col.references)}")
    return " ".join(parts)


def reference_target_sql(compiler: "Compiler", references: str) -> str:
    """Renders the table and column half of a REFERENCES clause."""
    ref_table, ref_column = references.rsplit(".", 1)
    quoted_table = compiler.quote_fully_qualified_identifier(ref_table)
    quoted_column = compiler.quote_identifier(ref_column)
    return f"{quoted_table} ({quoted_column})"


def build_create_table_sql(
    compiler: "Compiler",
    table_sql: str,
    columns: Dict[str, ColumnDef],
    if_not_exists: bool = False,
    options: Optional[TableOptions] = None,
    extras: Optional[List[str]] = None,
    constraints: Optional[Sequence[TableConstraint]] = None,
) -> str:
    """
    Renders a CREATE TABLE statement from typed column definitions using
    the given dialect compiler. `extras` are pre-rendered column parts
    appended after the declared columns. `constraints` are declared
    Check and ForeignKey table constraints, rendered after the
    constraints the columns themselves imply.
    """
    if not columns:
        raise ValueError("Cannot create a table with no columns.")

    primary_keys = [name for name, col in columns.items() if col.primary_key]
    autoincrement_cols = [name for name, col in columns.items() if col.autoincrement]
    if autoincrement_cols and len(primary_keys) > 1:
        raise ValueError(
            "autoincrement cannot be combined with a composite primary key."
        )

    column_parts: List[str] = []
    table_constraints: List[str] = []
    inline_pk = len(primary_keys) == 1

    for name, col in columns.items():
        column_parts.append(render_column_sql(compiler, name, col, inline_pk))

    if len(primary_keys) > 1:
        pk_sql = ", ".join(compiler.quote_identifier(c) for c in primary_keys)
        table_constraints.append(f"PRIMARY KEY ({pk_sql})")

    if not compiler.inline_references():
        for name, col in columns.items():
            if col.references is None:
                continue
            table_constraints.append(
                f"FOREIGN KEY ({compiler.quote_identifier(name)}) REFERENCES "
                f"{reference_target_sql(compiler, col.references)}"
            )

    if compiler.enum_strategy() == "check":
        table_name = bare_table_name(table_sql)
        for name, col in columns.items():
            if col.type_name == "ENUM":
                table_constraints.append(
                    enum_check_constraint_sql(compiler, table_name, name, col)
                )

    for declared in constraints or []:
        table_constraints.append(table_constraint_sql(compiler, declared))

    body = ", ".join(column_parts + list(extras or []) + table_constraints)
    suffix = compiler.compile_table_options(options)
    suffix_sql = f" {suffix}" if suffix else ""
    return compiler.compile_create_table(table_sql, body, suffix_sql, if_not_exists)


__all__ = [
    "Check",
    "ColumnDef",
    "ForeignKey",
    "Index",
    "TableConstraint",
    "TableOptions",
    "Integer",
    "BigInteger",
    "String",
    "Text",
    "Boolean",
    "Float",
    "Numeric",
    "Date",
    "Timestamp",
    "Json",
    "Enum",
    "build_create_table_sql",
    "check_constraint_sql",
    "collect_enum_types",
    "enum_check_constraint_sql",
    "foreign_key_constraint_sql",
    "table_constraint_sql",
    "Expression",
]
