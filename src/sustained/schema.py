"""
Typed column definitions and model-driven DDL.

Models declare their physical shape with tableColumns, a dict of column
name to ColumnDef. The dialect compiler maps each logical type to the
engine's SQL type, so one declaration generates CREATE TABLE for every
supported dialect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sustained.types import Expression

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
        default: Any = None,
        references: Optional[str] = None,
        autoincrement: bool = False,
        backfill: Any = None,
    ) -> None:
        if autoincrement and type_name not in ("INTEGER", "BIGINT"):
            raise ValueError("autoincrement requires an Integer or BigInteger column.")
        if autoincrement and not primary_key:
            raise ValueError("autoincrement requires primary_key=True.")
        if references is not None and "." not in references:
            raise ValueError("references must be a 'table.column' string.")
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
    if col.references is not None:
        ref_table, ref_column = col.references.rsplit(".", 1)
        quoted_table = compiler.quote_fully_qualified_identifier(ref_table)
        quoted_column = compiler.quote_identifier(ref_column)
        parts.append(f"REFERENCES {quoted_table} ({quoted_column})")
    return " ".join(parts)


def build_create_table_sql(
    compiler: "Compiler",
    table_sql: str,
    columns: Dict[str, ColumnDef],
    if_not_exists: bool = False,
    options: Optional[TableOptions] = None,
) -> str:
    """
    Renders a CREATE TABLE statement from typed column definitions using
    the given dialect compiler.
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

    body = ", ".join(column_parts + table_constraints)
    exists_sql = "IF NOT EXISTS " if if_not_exists else ""
    suffix = compiler.compile_table_options(options)
    suffix_sql = f" {suffix}" if suffix else ""
    return f"CREATE TABLE {exists_sql}{table_sql} ({body}){suffix_sql}"


__all__ = [
    "ColumnDef",
    "Index",
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
    "build_create_table_sql",
    "Expression",
]
