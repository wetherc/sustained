from .base import Compiler


class PostgresCompiler(Compiler):
    def quote_identifier(self, identifier: str) -> str:
        return f'"{identifier}"'

    def placeholder(self) -> str:
        # The %s style used by psycopg and psycopg2.
        return "%s"

    def quote_fully_qualified_identifier(self, identifier: str) -> str:
        return ".".join([self.quote_identifier(part) for part in identifier.split(".")])
