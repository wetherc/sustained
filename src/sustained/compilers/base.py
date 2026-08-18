import re
from typing import TYPE_CHECKING, Optional, Sequence, Union

from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Column,
    Func,
    Literal,
    Subquery,
    WindowExpression,
)
from sustained.types import Expression, SqlValue

if TYPE_CHECKING:
    from sustained.dialects import Dialects
    from sustained.schema import ColumnDef, TableOptions
    from sustained.types import CaseResult


# A plain identifier path such as "users", "users.id", or "db.dbo.users.id".
_IDENTIFIER_PATH_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$"
)

# Comparison operators accepted by the conditional clause builders. Anything
# else must be expressed with QueryBuilder.raw() so that intent is explicit.
_VALID_OPERATORS = frozenset(
    {
        "=",
        "!=",
        "<>",
        "<",
        "<=",
        ">",
        ">=",
        "LIKE",
        "NOT LIKE",
        "ILIKE",
        "NOT ILIKE",
        "IS",
        "IS NOT",
    }
)


class Compiler:
    def __init__(self, dialect: "Dialects") -> None:
        self._dialect = dialect

    def dialect_name(self) -> str:
        """The dialect's name, for error messages."""
        return self._dialect.name

    def quote_identifier(self, identifier: str) -> str:
        return identifier

    def quote_fully_qualified_identifier(self, identifier: str) -> str:
        return ".".join(self.quote_identifier(part) for part in identifier.split("."))

    def quote_column_reference(self, column: Union[str, Expression]) -> str:
        """
        Quotes a column reference for use inside a clause.

        Plain identifier paths are quoted per dialect. The star selector and
        anything more complex, such as a function call in a HAVING clause, is
        passed through unchanged. Expression objects are raw SQL.
        """
        if isinstance(column, Expression):
            return str(column)
        if not isinstance(column, str):
            raise TypeError(
                f"Column reference must be a string or Expression, got {type(column).__name__}."
            )
        if column == "*":
            return column
        if _IDENTIFIER_PATH_RE.match(column):
            return self.quote_fully_qualified_identifier(column)
        return column

    def validate_operator(self, operator: str) -> str:
        """
        Normalizes and validates a comparison operator.

        Raises:
            ValueError: If the operator is not a recognized SQL comparison
                operator. Raw predicates should use QueryBuilder.raw().
        """
        if not isinstance(operator, str):
            raise TypeError("Operator must be a string.")
        normalized = " ".join(operator.strip().upper().split())
        if normalized not in _VALID_OPERATORS:
            raise ValueError(
                f"Unsupported SQL operator: {operator!r}. "
                "Use QueryBuilder.raw() for raw SQL predicates."
            )
        return normalized

    def supports_qualify(self) -> bool:
        """Reports whether the dialect supports the QUALIFY clause."""
        return False

    def compile_with_keyword(self, recursive: bool) -> str:
        """Renders the WITH keyword, adding RECURSIVE where required."""
        return "WITH RECURSIVE" if recursive else "WITH"

    def compile_distinct_on(self, columns_sql: "list[str]") -> str:
        """
        Renders DISTINCT ON. A Postgres extension also supported by DuckDB;
        other dialects raise.
        """
        from sustained.exceptions import DialectError

        raise DialectError(
            f"DISTINCT ON is not supported by the '{self._dialect.name}' dialect. "
            "Use a window function with a row filter instead."
        )

    def compile_locking(self, skip_locked: bool, nowait: bool) -> str:
        """
        Renders a FOR UPDATE locking clause. Dialects without it raise.
        """
        from sustained.exceptions import DialectError

        raise DialectError(
            f"FOR UPDATE is not supported by the '{self._dialect.name}' dialect."
        )

    def compile_explain(self, analyze: bool) -> str:
        """Renders the EXPLAIN prefix. Dialects without EXPLAIN raise."""
        return "EXPLAIN ANALYZE" if analyze else "EXPLAIN"

    def compile_like(self, column_sql: str, pattern_sql: str, operator: str) -> str:
        """
        Renders a LIKE or ILIKE predicate. ILIKE is a Postgres extension, so
        the base compiler emulates it by lowercasing both sides. Dialects
        with native ILIKE override this.
        """
        if operator == "ILIKE":
            return f"LOWER({column_sql}) LIKE LOWER({pattern_sql})"
        if operator == "NOT ILIKE":
            return f"LOWER({column_sql}) NOT LIKE LOWER({pattern_sql})"
        return f"{column_sql} {operator} {pattern_sql}"

    def placeholder(self) -> str:
        return "?"

    def format_value(self, value: SqlValue) -> str:
        if isinstance(value, Expression):
            return str(value)
        if value is None:
            return "NULL"
        # bool must be checked before int because bool subclasses int.
        if isinstance(value, bool):
            return self.compile_boolean(value)
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            escaped_value = value.replace("'", "''")
            return f"'{escaped_value}'"
        raise TypeError(
            f"Cannot render a value of type {type(value).__name__} as a SQL literal."
        )

    def compile_boolean(self, value: bool) -> str:
        return "TRUE" if value else "FALSE"

    def compile_upsert_statement(
        self,
        table_sql: str,
        column_names: "list[str]",
        row_values_sql: "list[str]",
        conflict_columns: "list[str]",
        action: str,
        update_columns: "list[str]",
    ) -> str:
        """
        Renders an insert with conflict handling. The base form is the
        ON CONFLICT syntax shared by Postgres, SQLite, and DuckDB. Dialects
        with a different upsert statement override this.
        """
        columns_sql = ", ".join(self.quote_identifier(c) for c in column_names)
        sql = (
            f"INSERT INTO {table_sql} ({columns_sql}) "
            f"VALUES {', '.join(row_values_sql)}"
        )
        conflict_sql = ", ".join(self.quote_identifier(c) for c in conflict_columns)
        if action == "ignore":
            return f"{sql} ON CONFLICT ({conflict_sql}) DO NOTHING"
        assignments = ", ".join(
            f"{self.quote_identifier(c)} = EXCLUDED.{self.quote_identifier(c)}"
            for c in update_columns
        )
        return f"{sql} ON CONFLICT ({conflict_sql}) DO UPDATE SET {assignments}"

    # Logical column types mapped to this dialect's SQL types. Dialects
    # override entries as needed.
    _TYPE_MAP = {
        "INTEGER": "INTEGER",
        "BIGINT": "BIGINT",
        "VARCHAR": "VARCHAR",
        "TEXT": "TEXT",
        "BOOLEAN": "BOOLEAN",
        "FLOAT": "DOUBLE PRECISION",
        "NUMERIC": "NUMERIC",
        "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP",
        "JSON": "JSON",
    }

    def enum_strategy(self) -> str:
        """
        How this dialect renders an enum column. One of:

        - 'native': a named type object, created with CREATE TYPE and
          referenced by name (Postgres, DuckDB).
        - 'inline': the value list written into the column type
          (MySQL ENUM('a', 'b')).
        - 'check': VARCHAR sized to the longest value, held to the list
          by a named CHECK constraint (ANSI, SQLite, MSSQL).

        Presto and Athena refuse enum columns in validate_column_def,
        so their strategy is never consulted.
        """
        return "check"

    def compile_column_type(self, column: "ColumnDef") -> str:
        """
        Renders a ColumnDef's logical type as this dialect's SQL type.
        """
        if column.type_name == "ENUM":
            return self.compile_enum_column_type(column)
        base = self._TYPE_MAP.get(column.type_name)
        if base is None:
            raise ValueError(f"Unknown column type: {column.type_name!r}.")
        if column.type_name == "VARCHAR" and column.length is not None:
            return f"{base}({column.length})"
        if column.type_name == "NUMERIC" and column.precision is not None:
            return f"{base}({column.precision}, {column.scale})"
        return base

    def compile_enum_column_type(self, column: "ColumnDef") -> str:
        """
        Renders an ENUM column's type per the dialect's enum strategy.
        """
        assert column.enum_name is not None and column.enum_values is not None
        strategy = self.enum_strategy()
        if strategy == "native":
            return self.quote_identifier(column.enum_name)
        if strategy == "inline":
            values_sql = ", ".join(self.format_value(v) for v in column.enum_values)
            return f"ENUM({values_sql})"
        longest = max(len(v) for v in column.enum_values)
        return f"{self._TYPE_MAP['VARCHAR']}({longest})"

    def compile_create_enum_type(self, name: str, values: "list[str]") -> str:
        """
        Renders CREATE TYPE for a named enum, on dialects that have one.
        """
        from sustained.exceptions import DialectError

        raise DialectError(
            f"The '{self._dialect.name}' dialect has no named enum types. "
            "Enum columns render per the dialect's enum strategy instead."
        )

    def compile_drop_enum_type(self, name: str, if_exists: bool = False) -> str:
        """
        Renders DROP TYPE for a named enum, on dialects that have one.
        """
        from sustained.exceptions import DialectError

        raise DialectError(
            f"The '{self._dialect.name}' dialect has no named enum types " "to drop."
        )

    def compile_add_enum_value(self, name: str, value: str) -> str:
        """
        Renders the statement that appends one value to a named enum
        type, on dialects that can.
        """
        from sustained.exceptions import DialectError

        raise DialectError(
            f"The '{self._dialect.name}' dialect cannot add a value to an "
            "enum type in place."
        )

    def compile_identity(self) -> str:
        """
        Renders the identity modifier for an autoincrement column. An empty
        string means the engine generates values without a modifier, as
        SQLite does for INTEGER PRIMARY KEY. Dialects without identity
        columns raise.
        """
        return ""

    def compile_add_column(self, table_sql: str, column_sql: str) -> str:
        """Renders an ALTER TABLE statement that adds one column."""
        return f"ALTER TABLE {table_sql} ADD COLUMN {column_sql}"

    def compile_drop_column(self, table_sql: str, column_name: str) -> str:
        """Renders an ALTER TABLE statement that drops one column."""
        quoted = self.quote_identifier(column_name)
        return f"ALTER TABLE {table_sql} DROP COLUMN {quoted}"

    def compile_rename_column(
        self, table_sql: str, old_name: str, new_name: str
    ) -> str:
        """Renders a column rename."""
        old_sql = self.quote_identifier(old_name)
        new_sql = self.quote_identifier(new_name)
        return f"ALTER TABLE {table_sql} RENAME COLUMN {old_sql} TO {new_sql}"

    def inline_references(self) -> bool:
        """
        Reports whether a REFERENCES clause written beside a column
        definition creates a foreign key. MySQL parses one and creates
        nothing, so it says no and takes its foreign keys as table
        constraints instead.
        """
        return True

    def compile_add_foreign_key(
        self,
        table_sql: str,
        constraint: str,
        column: "Union[str, Sequence[str]]",
        ref_table_sql: str,
        ref_column: "Union[str, Sequence[str]]",
        on_delete: Optional[str] = None,
        on_update: Optional[str] = None,
    ) -> str:
        """
        Renders a named foreign key added to an existing table. `column`
        and `ref_column` take one name or a matching sequence of names
        for a composite key. Actions render as given; validate them
        before calling.
        """
        columns = (column,) if isinstance(column, str) else tuple(column)
        targets = (ref_column,) if isinstance(ref_column, str) else tuple(ref_column)
        columns_sql = ", ".join(self.quote_identifier(c) for c in columns)
        sql = (
            f"ALTER TABLE {table_sql} ADD CONSTRAINT "
            f"{self.quote_identifier(constraint)} FOREIGN KEY "
            f"({columns_sql}) REFERENCES {ref_table_sql}"
        )
        if targets:
            # An empty target list means the key references the target
            # table's primary key, so the column list is left off.
            targets_sql = ", ".join(self.quote_identifier(c) for c in targets)
            sql += f" ({targets_sql})"
        if on_delete is not None:
            sql += f" ON DELETE {on_delete}"
        if on_update is not None:
            sql += f" ON UPDATE {on_update}"
        return sql

    def compile_add_check(
        self, table_sql: str, constraint: str, expression: str
    ) -> str:
        """
        Renders a named CHECK constraint added to an existing table. The
        expression is SQL and renders as written.
        """
        return (
            f"ALTER TABLE {table_sql} ADD CONSTRAINT "
            f"{self.quote_identifier(constraint)} CHECK ({expression})"
        )

    def compile_drop_foreign_key(self, table_sql: str, constraint: str) -> str:
        """Renders the statement that takes back an added foreign key."""
        return (
            f"ALTER TABLE {table_sql} DROP CONSTRAINT "
            f"{self.quote_identifier(constraint)}"
        )

    def compile_drop_constraint(self, table_sql: str, constraint: str) -> str:
        """Renders the statement that drops a named table constraint."""
        return (
            f"ALTER TABLE {table_sql} DROP CONSTRAINT "
            f"{self.quote_identifier(constraint)}"
        )

    def compile_rename_table(self, old_sql: str, new_sql: str) -> str:
        """Renders a table rename."""
        return f"ALTER TABLE {old_sql} RENAME TO {new_sql}"

    def compile_create_index(
        self,
        index_name: str,
        table_sql: str,
        columns: "list[str]",
        unique: bool,
    ) -> str:
        """Renders a CREATE INDEX statement."""
        unique_sql = "UNIQUE " if unique else ""
        name_sql = self.quote_identifier(index_name)
        columns_sql = ", ".join(self.quote_identifier(c) for c in columns)
        return f"CREATE {unique_sql}INDEX {name_sql} ON {table_sql} ({columns_sql})"

    def compile_drop_index(self, index_name: str, table_sql: str) -> str:
        """Renders a DROP INDEX statement."""
        return f"DROP INDEX {self.quote_identifier(index_name)}"

    def compile_create_table(
        self, table_sql: str, body: str, suffix_sql: str, if_missing: bool
    ) -> str:
        """
        Renders a CREATE TABLE statement. With if_missing, the statement
        does nothing when the table is already there, which most engines
        spell as an IF NOT EXISTS clause.
        """
        exists_sql = "IF NOT EXISTS " if if_missing else ""
        return f"CREATE TABLE {exists_sql}{table_sql} ({body}){suffix_sql}"

    def supports_alter_column(self) -> bool:
        """
        Reports whether the dialect can change a column's type or
        nullability with ALTER TABLE. SQLite cannot and needs a table
        rebuild instead.
        """
        return False

    def supports_constraints(self) -> bool:
        """
        Reports whether the dialect supports column and table constraints
        such as PRIMARY KEY, UNIQUE, DEFAULT, and REFERENCES. Athena does
        not; its tables are files on object storage.
        """
        return True

    def supports_transactions(self) -> bool:
        """
        Reports whether the engine supports transactions. The migration
        runner wraps each migration in a transaction only when it does.
        """
        return True

    def supports_transactional_ddl(self) -> bool:
        """
        Reports whether a schema change taken back by a rollback really
        goes away. On most engines this follows supports_transactions(),
        so that is the default. MySQL is the exception: its transactions
        work for rows, but every DDL statement commits as it runs, and a
        migration that fails halfway leaves the statements before it in
        place.

        The migration runner reads this rather than supports_transactions()
        when it decides whether to wrap a migration and whether a failed
        migration needs a failure row recorded for repair().
        """
        return self.supports_transactions()

    def begin_transaction_sql(self) -> Optional[str]:
        """
        The statement that opens a transaction, or None on engines that
        have no transactions.

        A rehearsal opens and closes its transaction with these statements
        rather than with the driver's own calls, because drivers disagree:
        SQLite starts a transaction for INSERT but not for CREATE TABLE,
        and asyncpg runs in autocommit until a transaction is opened. On a
        driver that already opened one, an explicit BEGIN would warn, so
        the rehearsal rolls back first.
        """
        return "BEGIN" if self.supports_transactions() else None

    def rollback_transaction_sql(self) -> Optional[str]:
        """
        The statement that takes back the transaction begin_transaction_sql()
        opened, or None on engines that have no transactions.
        """
        return "ROLLBACK" if self.supports_transactions() else None

    def migration_lock_sql(self, name: str) -> "list[str]":
        """
        Statements that take an exclusive, session-scoped advisory lock so
        two migrators cannot run at once. An empty list means the engine
        has no such lock; SQLite and DuckDB serialize writers on their own,
        and Athena offers nothing to lock with.
        """
        return []

    def migration_unlock_sql(self, name: str) -> "list[str]":
        """Statements that release the advisory lock taken for migrations."""
        return []

    def validate_column_def(self, column: "ColumnDef") -> None:
        """
        Rejects ColumnDef features the dialect cannot express in DDL.
        The base compiler accepts everything.
        """

    def compile_table_options(self, options: Optional["TableOptions"]) -> str:
        """
        Renders the clause that follows the column list of CREATE TABLE:
        partitioning, storage location, and table properties. Dialects
        without these clauses raise when options are given.
        """
        if options is None:
            return ""
        from sustained.exceptions import DialectError

        raise DialectError(
            f"The '{self._dialect.name}' dialect does not support table "
            "options (partitioning, location, or table properties)."
        )

    def compile_alter_column_type(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        using: Optional[str] = None,
    ) -> "list[str]":
        """Renders statements that change a column's type."""
        from sustained.exceptions import DialectError

        raise DialectError(
            f"The '{self._dialect.name}' dialect cannot alter a column type "
            "in place."
        )

    def compile_alter_column_nullability(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        nullable: bool,
    ) -> "list[str]":
        """Renders statements that change a column's nullability."""
        from sustained.exceptions import DialectError

        raise DialectError(
            f"The '{self._dialect.name}' dialect cannot alter column "
            "nullability in place."
        )

    def compile_returning(self, columns_sql: str) -> str:
        """
        Renders a RETURNING clause for DML statements. Dialects without
        support raise DialectError.
        """
        return f"RETURNING {columns_sql}"

    def compile_ctas(self, table_sql: str, select_sql: str, temporary: bool) -> str:
        """
        Renders a CREATE TABLE ... AS statement. Dialects with a different
        shape raise DialectError.
        """
        keyword = "CREATE TEMPORARY TABLE" if temporary else "CREATE TABLE"
        return f"{keyword} {table_sql} AS {select_sql}"

    def compile_top(self, value: int) -> str:
        from sustained.exceptions import DialectError

        raise DialectError(
            f"TOP is not supported by the '{self._dialect.name}' dialect. Use limit() instead."
        )

    def compile_limit_offset(
        self,
        limit: Optional[int],
        offset: Optional[int],
        has_order_by: bool = False,
    ) -> str:
        parts = []
        if limit is not None:
            parts.append(f"LIMIT {limit}")
        if offset is not None:
            parts.append(f"OFFSET {offset}")
        return " ".join(parts)

    def compile_function(self, func: Func) -> str:
        """
        Renders a Func expression as a SQL string, translating the function
        name to the dialect's spelling when the registry defines one.
        """
        # Imported here because the dialects module imports the compilers
        # at module load time.
        from sustained.functions import FunctionRegistry

        function_name = FunctionRegistry.resolve_name(func.function_name, self._dialect)
        formatted_args = ", ".join(self._format_arg(arg) for arg in func.args)
        sql = f"{function_name}({formatted_args})"
        if func.alias:
            sql += f" AS {self.quote_identifier(func.alias)}"
        return sql

    def compile_aggregate(self, agg: AggregateExpression) -> str:
        """
        Renders an aggregate expression with dialect quoting for the column
        and the alias.
        """
        column = self.quote_column_reference(agg.column)
        sql = f"{agg.function_name}({column})"
        if agg.alias:
            sql += f" AS {self.quote_identifier(agg.alias)}"
        return sql

    def compile_window(self, window: WindowExpression) -> str:
        """
        Renders a window expression with dialect quoting for partition and
        order columns and the alias.
        """
        over_clauses = []
        if window.partition_by:
            partition_cols = ", ".join(
                self.quote_column_reference(c) for c in window.partition_by
            )
            over_clauses.append(f"PARTITION BY {partition_cols}")
        if window.order_by:
            order_cols = ", ".join(self._quote_order_entry(c) for c in window.order_by)
            over_clauses.append(f"ORDER BY {order_cols}")
        if window.frame:
            over_clauses.append(window.frame)
        over_sql = " ".join(over_clauses)
        args_sql = ", ".join(self._format_arg(arg) for arg in window.args)
        alias_sql = self.quote_identifier(window.alias)
        return f"{window.function_name}({args_sql}) OVER ({over_sql}) AS {alias_sql}"

    def _quote_order_entry(self, entry: str) -> str:
        """Quotes an ORDER BY entry that may carry an ASC or DESC suffix."""
        parts = entry.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].upper() in ("ASC", "DESC"):
            return f"{self.quote_column_reference(parts[0])} {parts[1].upper()}"
        return self.quote_column_reference(entry)

    def compile_case(self, case: CaseExpression) -> str:
        """
        Renders a CASE expression. Results go through the dialect's value
        formatting, so booleans and NULL render correctly per dialect.
        """
        sql = "CASE"
        for condition, result in case.whens:
            sql += f" WHEN {condition} THEN {self._format_case_result(result)}"
        sql += f" ELSE {self._format_case_result(case.else_result)}"
        sql += f" END AS {self.quote_identifier(case.alias)}"
        return sql

    def _format_case_result(self, result: "CaseResult") -> str:
        if isinstance(result, Column):
            return str(result)
        return self.format_value(result)

    def _format_arg(self, arg: SqlValue) -> str:
        """
        Formats a function argument for inclusion in the SQL string.

        Strings are treated as column references and quoted per dialect.
        Literal values must be wrapped in Literal(). Numbers, booleans, and
        None render as literals directly.
        """
        if isinstance(arg, Func):
            return self.compile_function(arg)
        if isinstance(arg, Literal):
            return self.format_value(arg.value)
        if isinstance(arg, AggregateExpression):
            return self.compile_aggregate(arg)
        if isinstance(
            arg,
            (
                Column,
                Expression,
                WindowExpression,
                CaseExpression,
                Subquery,
            ),
        ):
            return str(arg)
        if isinstance(arg, str):
            if arg == "*" or _IDENTIFIER_PATH_RE.match(arg):
                return self.quote_column_reference(arg)
            raise ValueError(
                f"Function argument {arg!r} is not a column name. "
                "Wrap literal values in Literal() or raw SQL in Column()."
            )
        if arg is None or isinstance(arg, (bool, int, float)):
            return self.format_value(arg)
        raise TypeError(
            f"Cannot render a function argument of type {type(arg).__name__}."
        )
