from .base import Compiler


class PostgresCompiler(Compiler):
    def quote_identifier(self, identifier: str) -> str:
        return f'"{identifier}"'

    def quote_fully_qualified_identifier(self, identifier: str) -> str:
        return ".".join([self.quote_identifier(part) for part in identifier.split(".")])
