"""
Typed steps for hand-written migrations.

A DdlStep names one schema change: add_column, create_index, drop_table,
and so on. A Migration whose up is a list of ddl steps renders per
dialect when it runs or when script() prints it, so one migration serves
every dialect the compilers cover. The rendered SQL is what the guards
and the destructive labels read, which a callable step never gives them.

Reversible steps carry their own inverse. A Migration whose up is all
reversible ddl steps derives its down step automatically: the inverses,
newest first. A step that cannot reverse (a drop, add_enum_value, raw
sql()) refuses the derivation, and the Migration then needs an explicit
down step or an explicit down=None.

Checksums are dialect-independent. A ddl step hashes as its canonical
signature, the operation name plus its arguments, so moving a project
from SQLite to Postgres never invalidates an applied migration.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Type,
    Union,
)

from sustained.schema import (
    Check,
    ColumnDef,
    ForeignKey,
    Index,
    TableConstraint,
    TableOptions,
    bare_table_name,
    build_create_table_sql,
    collect_enum_types,
)
from sustained.types import Expression

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.model import Model

TableRef = Union[str, Type["Model"]]

_Args = Dict[str, object]
_Renderer = Callable[[_Args, "Compiler"], List[str]]
_Inverter = Callable[[_Args], "DdlStep"]

_RENDERERS: Dict[str, _Renderer] = {}
_INVERTERS: Dict[str, _Inverter] = {}


class DdlStep:
    """
    One typed schema change inside a migration's up or down step.

    Build instances with the module's factories (add_column,
    create_index, sql, ...), not directly. A step renders to SQL through
    a dialect compiler when the migration runs, and hashes as a
    dialect-independent signature.
    """

    def __init__(self, op: str, args: _Args) -> None:
        if op not in _RENDERERS:
            raise ValueError(f"Unknown ddl operation: {op!r}.")
        self.op = op
        self.args = args

    def render(self, compiler: "Compiler") -> List[str]:
        """The SQL statements this step runs on the compiler's dialect."""
        return _RENDERERS[self.op](self.args, compiler)

    @property
    def reversible(self) -> bool:
        """Whether this step knows the step that takes it back."""
        return self.op in _INVERTERS

    def inverse(self) -> Optional["DdlStep"]:
        """
        The step that takes this one back, or None for a step that
        cannot reverse: a drop, add_enum_value, or raw sql().
        """
        invert = _INVERTERS.get(self.op)
        return None if invert is None else invert(self.args)

    def signature(self) -> str:
        """
        The canonical form the checksum hashes: the operation name and
        its arguments, serialized the same way on every dialect.
        """
        return json.dumps(
            {"op": self.op, "args": _canonical(self.args)},
            sort_keys=True,
            separators=(",", ":"),
        )

    def __repr__(self) -> str:
        parts = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
        return f"ddl.{self.op}({parts})"


def _canonical(value: object) -> object:
    """A JSON-serializable form of one step argument, stable across runs."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Expression):
        return {"$expression": value.value}
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, ColumnDef):
        # The comment key appears only when a comment is set. A column
        # without one serializes exactly as it did before comments
        # existed, so the checksums of applied migrations hold.
        comment = {} if value.comment is None else {"comment": value.comment}
        return {
            "$column": _canonical(
                {
                    **comment,
                    "type_name": value.type_name,
                    "length": value.length,
                    "precision": value.precision,
                    "scale": value.scale,
                    "primary_key": value.primary_key,
                    "nullable": value.nullable,
                    "unique": value.unique,
                    "default": value.default,
                    "references": value.references,
                    "autoincrement": value.autoincrement,
                    "backfill": value.backfill,
                    "enum_name": value.enum_name,
                    "enum_values": value.enum_values,
                }
            )
        }
    if isinstance(value, Check):
        return {"$check": {"name": value.name, "expression": value.expression}}
    if isinstance(value, ForeignKey):
        return {
            "$foreign_key": {
                "name": value.name,
                "columns": list(value.columns),
                "target_table": value.target_table,
                "target_columns": list(value.target_columns),
                "on_delete": value.on_delete,
                "on_update": value.on_update,
            }
        }
    if isinstance(value, Index):
        return {
            "$index": {
                "name": value.name,
                "columns": list(value.columns),
                "unique": value.unique,
            }
        }
    if isinstance(value, TableOptions):
        return {
            "$options": {
                "location": value.location,
                "partitioned_by": list(value.partitioned_by),
                "properties": dict(sorted(value.properties.items())),
            }
        }
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    raise TypeError(
        f"A ddl step cannot canonicalize {type(value).__name__!r} for its " "checksum."
    )


def _table_name(table: TableRef) -> str:
    """The dotted, unquoted name of a table, from a Model or a string."""
    if isinstance(table, str):
        if not table:
            raise ValueError("A ddl step needs a non-empty table name.")
        return table
    if not table.tableName:
        raise ValueError(f"Model '{table.__name__}' must define a tableName.")
    parts = [table.database, table.tableSchema, table.tableName]
    return ".".join(p for p in parts if p)


def _table_sql(args: _Args, compiler: "Compiler", key: str = "table") -> str:
    table = args[key]
    assert isinstance(table, str)
    return compiler.quote_fully_qualified_ddl_identifier(table)


def _op(op: str) -> Callable[[_Renderer], _Renderer]:
    def register(renderer: _Renderer) -> _Renderer:
        _RENDERERS[op] = renderer
        return renderer

    return register


def _inverse_of(op: str) -> Callable[[_Inverter], _Inverter]:
    def register(inverter: _Inverter) -> _Inverter:
        _INVERTERS[op] = inverter
        return inverter

    return register


# --- create_table / drop_table -----------------------------------------


def create_table(
    table: TableRef,
    columns: Optional[Dict[str, ColumnDef]] = None,
    constraints: Optional[Sequence[TableConstraint]] = None,
    options: Optional[TableOptions] = None,
    indexes: Optional[Sequence[Index]] = None,
) -> DdlStep:
    """
    Creates a table. Pass a Model to take its columns, constraints,
    options, and indexes, or a table name with an explicit columns
    mapping. On a dialect with named enum types, CREATE TYPE statements
    for the columns' enums render first; the derived down drops them
    after the table.

    A Model is read when the step is built, so a later model edit
    changes the migration's checksum. Pass explicit columns when the
    migration must outlive the model.
    """
    if not isinstance(table, str):
        if columns is None:
            columns = dict(table.tableColumns or {})
        if constraints is None:
            constraints = list(table.tableConstraints or [])
        if options is None:
            options = table.tableOptions
        if indexes is None:
            indexes = list(table.indexes or [])
    if not columns:
        raise ValueError(
            "create_table needs columns: pass a Model with tableColumns "
            "or an explicit columns mapping."
        )
    return DdlStep(
        "create_table",
        {
            "table": _table_name(table),
            "columns": dict(columns),
            "constraints": list(constraints or []),
            "options": options,
            "indexes": list(indexes or []),
        },
    )


@_op("create_table")
def _render_create_table(args: _Args, compiler: "Compiler") -> List[str]:
    columns = args["columns"]
    assert isinstance(columns, dict)
    table_sql = _table_sql(args, compiler)
    statements: List[str] = []
    if compiler.enum_strategy() == "native":
        for name, values in collect_enum_types(columns).items():
            statements.append(compiler.compile_create_enum_type(name, list(values)))
    constraints = args["constraints"]
    assert isinstance(constraints, list)
    options = args["options"]
    assert options is None or isinstance(options, TableOptions)
    statements.append(
        build_create_table_sql(
            compiler,
            table_sql,
            columns,
            options=options,
            constraints=constraints or None,
        )
    )
    from sustained.schema import column_comment_statements

    statements.extend(column_comment_statements(compiler, table_sql, columns))
    indexes = args["indexes"]
    assert isinstance(indexes, list)
    for index in indexes:
        statements.append(
            compiler.compile_create_index(
                index.name, table_sql, list(index.columns), index.unique
            )
        )
    return statements


@_inverse_of("create_table")
def _invert_create_table(args: _Args) -> DdlStep:
    columns = args["columns"]
    assert isinstance(columns, dict)
    table = args["table"]
    assert isinstance(table, str)
    return DdlStep(
        "drop_table",
        {"table": table, "enum_types": sorted(collect_enum_types(columns))},
    )


def drop_table(table: TableRef) -> DdlStep:
    """
    Drops a table. Irreversible: nothing here knows the columns to bring
    it back with, so the Migration needs an explicit down step or
    down=None. Pass a Model to also drop its enum types on dialects with
    named types; a bare name drops only the table.
    """
    enum_types: List[str] = []
    if not isinstance(table, str):
        enum_types = sorted(collect_enum_types(table.tableColumns or {}))
    return DdlStep(
        "drop_table", {"table": _table_name(table), "enum_types": enum_types}
    )


@_op("drop_table")
def _render_drop_table(args: _Args, compiler: "Compiler") -> List[str]:
    statements = [f"DROP TABLE {_table_sql(args, compiler)}"]
    enum_types = args["enum_types"]
    assert isinstance(enum_types, list)
    if compiler.enum_strategy() == "native":
        for name in enum_types:
            statements.append(compiler.compile_drop_enum_type(name))
    return statements


# --- columns ------------------------------------------------------------


def add_column(table: TableRef, name: str, column: ColumnDef) -> DdlStep:
    """
    Adds one column. An enum column on a check-strategy dialect renders
    as the column plus its named CHECK constraint, the same pair CREATE
    TABLE would put in the table body.
    """
    if not name:
        raise ValueError("add_column needs a column name.")
    return DdlStep(
        "add_column", {"table": _table_name(table), "name": name, "column": column}
    )


@_op("add_column")
def _render_add_column(args: _Args, compiler: "Compiler") -> List[str]:
    from sustained.schema import render_column_sql

    column = args["column"]
    assert isinstance(column, ColumnDef)
    name = args["name"]
    assert isinstance(name, str)
    table_sql = _table_sql(args, compiler)
    column_sql = render_column_sql(compiler, name, column, inline_pk=False)
    statements = [compiler.compile_add_column(table_sql, column_sql)]
    if (
        column.comment is not None
        and compiler.stores_column_comments()
        and not compiler.inline_column_comments()
    ):
        statements.extend(
            compiler.compile_set_column_comment(table_sql, name, column.comment)
        )
    if column.type_name == "ENUM" and compiler.enum_strategy() == "check":
        assert column.enum_values is not None
        constraint = _enum_check_name(table_sql, name)
        column_ref = compiler.quote_ddl_identifier(name)
        values_sql = ", ".join(compiler.format_value(v) for v in column.enum_values)
        statements.append(
            compiler.compile_add_check(
                table_sql, constraint, f"{column_ref} IN ({values_sql})"
            )
        )
    return statements


def _enum_check_name(table_sql: str, column_name: str) -> str:
    return f"ck_{bare_table_name(table_sql)}_{column_name}_enum"


@_inverse_of("add_column")
def _invert_add_column(args: _Args) -> DdlStep:
    column = args["column"]
    assert isinstance(column, ColumnDef)
    table = args["table"]
    name = args["name"]
    assert isinstance(table, str) and isinstance(name, str)
    enum_check = column.type_name == "ENUM"
    return DdlStep(
        "drop_column", {"table": table, "name": name, "drop_enum_check": enum_check}
    )


def drop_column(table: TableRef, name: str) -> DdlStep:
    """Drops one column. Irreversible."""
    if not name:
        raise ValueError("drop_column needs a column name.")
    return DdlStep(
        "drop_column",
        {"table": _table_name(table), "name": name, "drop_enum_check": False},
    )


@_op("drop_column")
def _render_drop_column(args: _Args, compiler: "Compiler") -> List[str]:
    name = args["name"]
    assert isinstance(name, str)
    table_sql = _table_sql(args, compiler)
    statements: List[str] = []
    if args["drop_enum_check"] and compiler.enum_strategy() == "check":
        statements.append(
            compiler.compile_drop_constraint(
                table_sql, _enum_check_name(table_sql, name)
            )
        )
    statements.append(compiler.compile_drop_column(table_sql, name))
    return statements


def rename_column(table: TableRef, old: str, new: str) -> DdlStep:
    """Renames one column. Reverses by renaming it back."""
    if not old or not new:
        raise ValueError("rename_column needs the old and the new name.")
    return DdlStep(
        "rename_column", {"table": _table_name(table), "old": old, "new": new}
    )


@_op("rename_column")
def _render_rename_column(args: _Args, compiler: "Compiler") -> List[str]:
    old = args["old"]
    new = args["new"]
    assert isinstance(old, str) and isinstance(new, str)
    return [compiler.compile_rename_column(_table_sql(args, compiler), old, new)]


@_inverse_of("rename_column")
def _invert_rename_column(args: _Args) -> DdlStep:
    return DdlStep(
        "rename_column",
        {"table": args["table"], "old": args["new"], "new": args["old"]},
    )


def set_column_comment(
    table: TableRef,
    name: str,
    comment: Optional[str],
    previous: Optional[str] = None,
    column: Optional[ColumnDef] = None,
) -> DdlStep:
    """
    Sets or clears one column's comment. None clears. Reverses by
    setting `previous`, which is the comment the column carries now:
    None when it has none. MySQL restates the column definition to
    change its comment, so there the step also needs the ColumnDef as
    `column`. Dialects that store no column comments raise at render.
    """
    if not name:
        raise ValueError("set_column_comment needs a column name.")
    return DdlStep(
        "set_column_comment",
        {
            "table": _table_name(table),
            "name": name,
            "comment": comment,
            "previous": previous,
            "column": column,
        },
    )


@_op("set_column_comment")
def _render_set_column_comment(args: _Args, compiler: "Compiler") -> List[str]:
    name = args["name"]
    comment = args["comment"]
    column = args["column"]
    assert isinstance(name, str)
    assert comment is None or isinstance(comment, str)
    assert column is None or isinstance(column, ColumnDef)
    return compiler.compile_set_column_comment(
        _table_sql(args, compiler), name, comment, column
    )


@_inverse_of("set_column_comment")
def _invert_set_column_comment(args: _Args) -> DdlStep:
    return DdlStep(
        "set_column_comment",
        {
            "table": args["table"],
            "name": args["name"],
            "comment": args["previous"],
            "previous": args["comment"],
            "column": args["column"],
        },
    )


def rename_table(old: TableRef, new: str) -> DdlStep:
    """Renames a table. Reverses by renaming it back."""
    if not new:
        raise ValueError("rename_table needs the new name.")
    return DdlStep("rename_table", {"old": _table_name(old), "new": new})


@_op("rename_table")
def _render_rename_table(args: _Args, compiler: "Compiler") -> List[str]:
    return [
        compiler.compile_rename_table(
            _table_sql(args, compiler, "old"), _table_sql(args, compiler, "new")
        )
    ]


@_inverse_of("rename_table")
def _invert_rename_table(args: _Args) -> DdlStep:
    return DdlStep("rename_table", {"old": args["new"], "new": args["old"]})


# --- constraints ----------------------------------------------------------


def add_foreign_key(table: TableRef, foreign_key: ForeignKey) -> DdlStep:
    """Adds a named foreign key. Reverses by dropping it by name."""
    return DdlStep(
        "add_foreign_key",
        {"table": _table_name(table), "foreign_key": foreign_key},
    )


@_op("add_foreign_key")
def _render_add_foreign_key(args: _Args, compiler: "Compiler") -> List[str]:
    fk = args["foreign_key"]
    assert isinstance(fk, ForeignKey)
    return [
        compiler.compile_add_foreign_key(
            _table_sql(args, compiler),
            fk.name,
            fk.columns,
            compiler.quote_fully_qualified_ddl_identifier(fk.target_table),
            fk.target_columns,
            fk.on_delete,
            fk.on_update,
        )
    ]


@_inverse_of("add_foreign_key")
def _invert_add_foreign_key(args: _Args) -> DdlStep:
    fk = args["foreign_key"]
    assert isinstance(fk, ForeignKey)
    return DdlStep("drop_foreign_key", {"table": args["table"], "name": fk.name})


def drop_foreign_key(table: TableRef, name: str) -> DdlStep:
    """Drops a named foreign key. Irreversible."""
    if not name:
        raise ValueError("drop_foreign_key needs the constraint name.")
    return DdlStep("drop_foreign_key", {"table": _table_name(table), "name": name})


@_op("drop_foreign_key")
def _render_drop_foreign_key(args: _Args, compiler: "Compiler") -> List[str]:
    name = args["name"]
    assert isinstance(name, str)
    return [compiler.compile_drop_foreign_key(_table_sql(args, compiler), name)]


def add_check(table: TableRef, check: Check) -> DdlStep:
    """Adds a named CHECK constraint. Reverses by dropping it by name."""
    return DdlStep("add_check", {"table": _table_name(table), "check": check})


@_op("add_check")
def _render_add_check(args: _Args, compiler: "Compiler") -> List[str]:
    check = args["check"]
    assert isinstance(check, Check)
    return [
        compiler.compile_add_check(
            _table_sql(args, compiler), check.name, check.expression
        )
    ]


@_inverse_of("add_check")
def _invert_add_check(args: _Args) -> DdlStep:
    check = args["check"]
    assert isinstance(check, Check)
    return DdlStep("drop_constraint", {"table": args["table"], "name": check.name})


def drop_constraint(table: TableRef, name: str) -> DdlStep:
    """Drops a named table constraint. Irreversible."""
    if not name:
        raise ValueError("drop_constraint needs the constraint name.")
    return DdlStep("drop_constraint", {"table": _table_name(table), "name": name})


@_op("drop_constraint")
def _render_drop_constraint(args: _Args, compiler: "Compiler") -> List[str]:
    name = args["name"]
    assert isinstance(name, str)
    return [compiler.compile_drop_constraint(_table_sql(args, compiler), name)]


# --- indexes --------------------------------------------------------------


def create_index(table: TableRef, index: Index) -> DdlStep:
    """Creates a named index. Reverses by dropping it by name."""
    return DdlStep("create_index", {"table": _table_name(table), "index": index})


@_op("create_index")
def _render_create_index(args: _Args, compiler: "Compiler") -> List[str]:
    index = args["index"]
    assert isinstance(index, Index)
    return [
        compiler.compile_create_index(
            index.name, _table_sql(args, compiler), list(index.columns), index.unique
        )
    ]


@_inverse_of("create_index")
def _invert_create_index(args: _Args) -> DdlStep:
    index = args["index"]
    assert isinstance(index, Index)
    return DdlStep("drop_index", {"table": args["table"], "name": index.name})


def drop_index(table: TableRef, name: str) -> DdlStep:
    """Drops a named index. Irreversible."""
    if not name:
        raise ValueError("drop_index needs the index name.")
    return DdlStep("drop_index", {"table": _table_name(table), "name": name})


@_op("drop_index")
def _render_drop_index(args: _Args, compiler: "Compiler") -> List[str]:
    name = args["name"]
    assert isinstance(name, str)
    return [compiler.compile_drop_index(name, _table_sql(args, compiler))]


# --- enum types -------------------------------------------------------------


def create_enum(name: str, *values: str) -> DdlStep:
    """
    Creates a named enum type, on dialects that have one. Reverses by
    dropping the type. Check-strategy dialects have no type object and
    raise DialectError at render; their enum lives in the column.
    """
    if not name:
        raise ValueError("create_enum needs a type name.")
    if not values:
        raise ValueError(f"Enum '{name}' needs at least one value.")
    return DdlStep("create_enum", {"name": name, "values": list(values)})


@_op("create_enum")
def _render_create_enum(args: _Args, compiler: "Compiler") -> List[str]:
    name = args["name"]
    values = args["values"]
    assert isinstance(name, str) and isinstance(values, list)
    return [compiler.compile_create_enum_type(name, values)]


@_inverse_of("create_enum")
def _invert_create_enum(args: _Args) -> DdlStep:
    return DdlStep("drop_enum", {"name": args["name"]})


def drop_enum(name: str) -> DdlStep:
    """Drops a named enum type. Irreversible."""
    if not name:
        raise ValueError("drop_enum needs a type name.")
    return DdlStep("drop_enum", {"name": name})


@_op("drop_enum")
def _render_drop_enum(args: _Args, compiler: "Compiler") -> List[str]:
    name = args["name"]
    assert isinstance(name, str)
    return [compiler.compile_drop_enum_type(name)]


def add_enum_value(name: str, value: str) -> DdlStep:
    """
    Appends one value to a named enum type. Irreversible: Postgres has
    no DROP VALUE, so the Migration needs an explicit down step or
    down=None.
    """
    if not name or not value:
        raise ValueError("add_enum_value needs the type name and the value.")
    return DdlStep("add_enum_value", {"name": name, "value": value})


@_op("add_enum_value")
def _render_add_enum_value(args: _Args, compiler: "Compiler") -> List[str]:
    name = args["name"]
    value = args["value"]
    assert isinstance(name, str) and isinstance(value, str)
    return [compiler.compile_add_enum_value(name, value)]


# --- escape hatch -------------------------------------------------------


def sql(text: str) -> DdlStep:
    """
    One raw SQL statement, for what the typed steps do not cover. It
    renders as written on every dialect and cannot reverse, so a
    migration that carries one needs an explicit down step or down=None.
    """
    if not text or not text.strip():
        raise ValueError("sql() needs a statement.")
    return DdlStep("sql", {"text": text.strip()})


@_op("sql")
def _render_sql(args: _Args, compiler: "Compiler") -> List[str]:
    text = args["text"]
    assert isinstance(text, str)
    return [text]


__all__ = [
    "DdlStep",
    "create_table",
    "drop_table",
    "add_column",
    "drop_column",
    "rename_column",
    "rename_table",
    "set_column_comment",
    "add_foreign_key",
    "drop_foreign_key",
    "add_check",
    "drop_constraint",
    "create_index",
    "drop_index",
    "create_enum",
    "drop_enum",
    "add_enum_value",
    "sql",
]
