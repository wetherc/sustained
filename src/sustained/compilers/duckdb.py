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

    def compile_identity(self) -> str:
        from sustained.exceptions import DialectError

        raise DialectError(
            "DuckDB has no identity columns. Use a sequence with a DEFAULT "
            "expression instead."
        )
