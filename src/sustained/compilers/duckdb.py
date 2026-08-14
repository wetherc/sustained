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
