"""
The AWS Athena dialect.

Athena runs a Trino-based engine over files in S3, so it inherits the
Presto compiler's query behavior and replaces everything that touches
storage. Tables have no constraints, no indexes, and no identity columns.
DDL takes Athena's spellings: STRING for every string column, since
Iceberg tables reject VARCHAR, ADD COLUMNS instead
of ADD COLUMN, CHANGE COLUMN for type changes, and a PARTITIONED BY,
LOCATION, and TBLPROPERTIES clause after the column list. DDL identifiers
quote with backticks for Athena's Hive DDL parser, while query
identifiers keep Presto's double quotes for the Trino engine. Placeholders
render as ?, which pyathena sends as native execution parameters when
pyathena.paramstyle is set to "qmark". Sustained passes parameters as a
tuple, which pyathena's default pyformat style refuses: it takes a dict
only. Every parameter travels to the service as a string, since the
Athena API takes nothing else, and the service pastes each string into
the statement as written, so each value travels as its SQL literal: a
string in single quotes, a number bare, a date as DATE '...'. A None
parameter becomes a literal NULL in the statement, because NULL has no
parameter spelling. The API takes a parameter of at most 1024
characters, so a string whose literal is longer travels as several
parameters joined with ||, each one a string literal within the limit.

Upserts, UPDATE, DELETE, and in-place column changes only work on Iceberg
tables (created with the table_type=ICEBERG property).
"""

from typing import TYPE_CHECKING, Optional, Sequence, Union

from sustained.exceptions import DialectError

from .presto import PrestoCompiler

if TYPE_CHECKING:
    from sustained.schema import ColumnDef, ColumnState, IndexColumn, TableOptions
    from sustained.types import SqlValue

# The longest string Athena's StartQueryExecution API accepts as one
# execution parameter.
_MAX_PARAMETER_LENGTH = 1024


def _execution_parameter(compiler: "AthenaCompiler", value: "SqlValue") -> str:
    """
    One parameter as the string Athena's API wants. The service pastes
    each value into the statement as text, so a string needs its quotes
    and a date or timestamp needs its type keyword. The values render as
    the dialect's literals: a string in single quotes with each quote
    doubled, a number bare, a boolean as TRUE or FALSE, a date as
    DATE '...', and a timestamp as TIMESTAMP '...'.
    """
    if isinstance(value, (bytes, bytearray)):
        raise DialectError(
            "Athena execution parameters cannot carry binary values. "
            "Write the value as literal SQL instead."
        )
    return compiler.format_value(value)


def _literal_length(value: str) -> int:
    """
    The length of a string's literal: the text, one more character for
    each quote it doubles, and the two enclosing quotes.
    """
    return len(value) + value.count("'") + 2


def _needs_rewrite(value: "SqlValue") -> bool:
    """
    Whether Athena's API would refuse the value as one execution
    parameter. The API has no spelling for NULL, and it takes a
    parameter only when it is at most 1024 characters long.
    """
    if value is None:
        return True
    return isinstance(value, str) and _literal_length(value) > _MAX_PARAMETER_LENGTH


def _split_string(value: str) -> "list[str]":
    """
    Splits a string into pieces whose literals each fit in one
    execution parameter. A quote counts twice, because its literal
    doubles it.
    """
    chunks = []
    current: list[str] = []
    size = 2
    for char in value:
        width = 2 if char == "'" else 1
        if size + width > _MAX_PARAMETER_LENGTH:
            chunks.append("".join(current))
            current, size = [], 2
        current.append(char)
        size += width
    chunks.append("".join(current))
    return chunks


def _rewrite_parameters(
    sql: str, params: "tuple[SqlValue, ...]"
) -> "tuple[str, tuple[SqlValue, ...]]":
    """
    Rewrites each placeholder bound to a value Athena's API would refuse
    as one parameter, keeping the rest. None becomes a literal NULL. A
    string whose literal is too long has its placeholder replaced by one
    placeholder per piece, joined with ||. The scan tracks quoted
    regions, so a question mark inside a string literal or a quoted
    identifier stays put.
    """
    from sustained.rendering import split_value_markers

    pieces = split_value_markers(sql)
    kept: list[SqlValue] = []
    out = [pieces[0]]
    for value, piece in zip(params, pieces[1:]):
        if value is None:
            out.append("NULL")
        elif _needs_rewrite(value):
            chunks = _split_string(str(value))
            out.append("(" + " || ".join(["?"] * len(chunks)) + ")")
            kept.extend(chunks)
        else:
            out.append("?")
            kept.append(value)
        out.append(piece)
    return "".join(out), tuple(kept)


class AthenaCompiler(PrestoCompiler):

    # Athena routes DDL through a Hive parser that takes backticks or
    # bare names only. A double-quoted identifier makes Athena try its
    # Trino parser, which has no LOCATION or TBLPROPERTIES clause, so
    # every CREATE and ALTER with one fails to parse. Queries and MERGE
    # run on the Trino engine and keep Presto's double quotes.
    _DDL_IDENT_QUOTES = ("`", "`")

    _TYPE_MAP = {
        "INTEGER": "INT",
        "BIGINT": "BIGINT",
        "VARCHAR": "STRING",
        "TEXT": "STRING",
        "BOOLEAN": "BOOLEAN",
        "FLOAT": "DOUBLE",
        "NUMERIC": "DECIMAL",
        "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP",
        "BINARY": "BINARY",
        "JSON": "STRING",
    }

    _supports_alter_column = True
    _supports_transactions = False

    def compile_column_type(self, column: "ColumnDef") -> str:
        # Every VARCHAR renders as STRING. Iceberg tables reject VARCHAR
        # outright ("Unsupported Hive type: VARCHAR, use string instead"),
        # and Athena enforces no length on the columns that would take it,
        # so the declared length only documents intent. The engine reports
        # a STRING column back as varchar; normalize_diff_type() folds the
        # two together so the column never drifts against its own DDL.
        if column.type_name == "VARCHAR":
            return "STRING"
        return super().compile_column_type(column)

    def prepare_execution(
        self, sql: str, params: "tuple[SqlValue, ...]"
    ) -> "tuple[str, tuple[SqlValue, ...]]":
        # Athena execution parameters travel to the service as strings;
        # boto3 rejects any other type before the query starts, and the
        # service substitutes each string into the statement as written.
        # NULL has no parameter spelling at all, so a None parameter's
        # placeholder is rewritten to a literal NULL in the statement.
        # The API refuses a parameter longer than 1024 characters, so a
        # string whose literal is longer is split across several
        # parameters joined with ||.
        if any(_needs_rewrite(value) for value in params):
            sql, params = _rewrite_parameters(sql, params)
        return sql, tuple(_execution_parameter(self, value) for value in params)

    def normalize_diff_type(self, type_name: str) -> str:
        # Athena stores every string column as STRING and reports it back
        # as varchar, so the two logical types are one type to a diff.
        if type_name == "VARCHAR":
            return "TEXT"
        return type_name

    def validate_column_def(self, column: "ColumnDef") -> None:
        if column.type_name == "ENUM":
            raise self._unsupported(
                "enum types",
                "It enforces no constraints, so it cannot keep an enum "
                "column's value list. Use String() and validate values in "
                "the application.",
            )
        problems = []
        if column.primary_key:
            problems.append("a primary key")
        if column.unique:
            problems.append("a unique constraint")
        if column.default is not None:
            problems.append("a default value")
        if column.references is not None:
            problems.append("a foreign key")
        if not column.nullable and not column.primary_key:
            problems.append("NOT NULL")
        if problems:
            raise DialectError(
                f"Athena tables cannot declare {', '.join(problems)}. "
                "Remove the constraint from the column definition; Athena "
                "stores tables as files and enforces no constraints."
            )

    def compile_set_column_comment(
        self,
        table_sql: str,
        column_name: str,
        comment: Optional[str],
        column: Optional["ColumnDef"] = None,
        state: Optional["ColumnState"] = None,
    ) -> "list[str]":
        raise DialectError(
            "Athena cannot change a column comment in place. Declare the "
            "comment on the model so CREATE TABLE carries it, or write "
            "the CHANGE COLUMN statement by hand in a Migration."
        )

    def compile_table_options(self, options: Optional["TableOptions"]) -> str:
        if options is None:
            return ""
        parts = []
        if options.partitioned_by:
            # Entries pass through unquoted so Iceberg partition transforms
            # such as day(created_at) stay intact.
            columns = ", ".join(options.partitioned_by)
            parts.append(f"PARTITIONED BY ({columns})")
        if options.location:
            escaped = options.location.replace("'", "''")
            parts.append(f"LOCATION '{escaped}'")
        if options.properties:
            props = ", ".join(
                f"{self.format_value(k)}={self.format_value(str(v))}"
                for k, v in options.properties.items()
            )
            parts.append(f"TBLPROPERTIES ({props})")
        return " ".join(parts)

    def compile_upsert_statement(
        self,
        table_sql: str,
        column_names: "list[str]",
        row_values_sql: "list[str]",
        conflict_columns: "list[str]",
        action: str,
        update_columns: "list[str]",
    ) -> str:
        # Athena supports MERGE INTO on Iceberg tables. Trino's MERGE
        # grammar wants unqualified column names on the left of SET.
        return self.compile_merge_upsert(
            table_sql,
            column_names,
            row_values_sql,
            conflict_columns,
            action,
            update_columns,
        )

    def compile_ctas(self, table_sql: str, select_sql: str, temporary: bool) -> str:
        if temporary:
            raise self._unsupported("temporary tables")
        return super().compile_ctas(table_sql, select_sql, temporary)

    def compile_add_column(self, table_sql: str, column_sql: str) -> str:
        # Athena spells this ADD COLUMNS with a parenthesized list.
        return f"ALTER TABLE {table_sql} ADD COLUMNS ({column_sql})"

    def compile_rename_column(
        self, table_sql: str, old_name: str, new_name: str
    ) -> str:
        raise DialectError(
            "Athena renames columns with ALTER TABLE ... CHANGE COLUMN, "
            "which needs the column type. Write the statement by hand in "
            "a Migration."
        )

    def compile_rename_table(self, old_sql: str, new_sql: str) -> str:
        raise self._unsupported("renaming tables")

    def compile_create_index(
        self,
        index_name: str,
        table_sql: str,
        columns: "Sequence[Union[str, IndexColumn]]",
        unique: bool,
        where: Optional[str] = None,
    ) -> str:
        raise self._unsupported(
            "indexes",
            "Remove the model's indexes declaration; use partitioning "
            "through table options instead.",
        )

    def compile_drop_index(self, index_name: str, table_sql: str) -> str:
        raise self._unsupported("indexes")

    def rebuild_strategy(self) -> str:
        # An Iceberg table takes CHANGE COLUMN, so Athena alters in place
        # rather than taking Presto's refusal.
        return "alter"

    def compile_alter_column_type(
        self,
        table_sql: str,
        column_name: str,
        column: "ColumnState",
        using: Optional[str] = None,
    ) -> "list[str]":
        # Iceberg tables allow widening type changes: int to bigint, float
        # to double, and growing a decimal's precision.
        if using is not None:
            raise DialectError(
                "Athena cannot cast values while changing a column type. "
                "Remove the type_casts hint."
            )
        quoted = self.quote_ddl_identifier(column_name)
        return [
            f"ALTER TABLE {table_sql} CHANGE COLUMN {quoted} {quoted} "
            f"{column.type_sql}"
        ]

    def compile_alter_column_nullability(
        self,
        table_sql: str,
        column_name: str,
        column: "ColumnState",
    ) -> "list[str]":
        raise DialectError(
            "Athena columns are always nullable; nullability cannot change."
        )
