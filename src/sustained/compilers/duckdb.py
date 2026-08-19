from typing import Optional

from .base import Compiler


class DuckDbCompiler(Compiler):
    """
    Compiler for DuckDB. DuckDB follows Postgres syntax closely: double
    quoted identifiers, native ILIKE, RETURNING, ON CONFLICT upserts, and
    CREATE TABLE AS. Its Python driver uses qmark placeholders, which the
    base compiler already emits.
    """

    def quote_identifier(self, identifier: str) -> str:
        return f'"{identifier}"'

    def compile_like(self, column_sql: str, pattern_sql: str, operator: str) -> str:
        # DuckDB supports ILIKE natively.
        return f"{column_sql} {operator} {pattern_sql}"

    def supports_qualify(self) -> bool:
        return True

    def compile_distinct_on(self, columns_sql: "list[str]") -> str:
        return f"DISTINCT ON ({', '.join(columns_sql)})"

    def supports_alter_column(self) -> bool:
        return True

    def compile_alter_column_type(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        using: "str | None" = None,
    ) -> "list[str]":
        column_sql = self.quote_identifier(column_name)
        return [
            f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} "
            f"SET DATA TYPE {type_sql}"
        ]

    def compile_alter_column_nullability(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        nullable: bool,
    ) -> "list[str]":
        column_sql = self.quote_identifier(column_name)
        action = "DROP NOT NULL" if nullable else "SET NOT NULL"
        return [f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} {action}"]

    def compile_backfill(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        filler_sql: str,
    ) -> "list[str]":
        # An UPDATE followed by SET NOT NULL in the same transaction fails
        # with "Cannot create index with outstanding updates". Rewriting
        # the column through USING fills the NULLs without an UPDATE.
        column_sql = self.quote_identifier(column_name)
        return [
            f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} SET DATA "
            f"TYPE {type_sql} USING coalesce({column_sql}, {filler_sql})"
        ]

    def compile_identity(self) -> str:
        from sustained.exceptions import DialectError

        raise DialectError(
            "DuckDB has no identity columns. Use a sequence with a DEFAULT "
            "expression instead."
        )

    def enum_strategy(self) -> str:
        return "native"

    def compile_create_enum_type(self, name: str, values: "list[str]") -> str:
        values_sql = ", ".join(self.format_value(v) for v in values)
        return f"CREATE TYPE {self.quote_identifier(name)} AS ENUM ({values_sql})"

    def compile_drop_enum_type(self, name: str, if_exists: bool = False) -> str:
        exists_sql = "IF EXISTS " if if_exists else ""
        return f"DROP TYPE {exists_sql}{self.quote_identifier(name)}"

    def driver_transaction_control(self) -> bool:
        # The duckdb driver autocommits every statement and gives every
        # cursor its own session, so transaction() runs BEGIN, COMMIT, and
        # ROLLBACK itself on the one cursor the block shares.
        return False

    def savepoint_sql(self, name: str) -> Optional[str]:
        # DuckDB has transactions but no savepoints, so transaction()
        # cannot nest on it.
        return None

    def rollback_savepoint_sql(self, name: str) -> Optional[str]:
        return None

    def release_savepoint_sql(self, name: str) -> Optional[str]:
        return None

    def compile_add_enum_value(self, name: str, value: str) -> str:
        from sustained.exceptions import DialectError

        raise DialectError(
            "DuckDB cannot add a value to an enum type in place. Create a "
            "new type, cast the column with ALTER COLUMN ... SET DATA TYPE, "
            "then drop the old type."
        )
