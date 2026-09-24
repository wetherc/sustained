from .base import Compiler


class DefaultCompiler(Compiler):
    """
    Compiler for the default dialect, which runs against SQLite. Queries
    write identifiers bare, as the base compiler does. DDL quotes every
    identifier with double quotes, which SQLite and ANSI SQL both take,
    so a table or column named after a keyword such as order still
    creates, rebuilds, and drops.
    """

    def quote_ddl_identifier(self, identifier: str) -> str:
        # A double quote inside the name doubles, so a name can never end
        # the quoted span early.
        return '"{}"'.format(identifier.replace('"', '""'))
