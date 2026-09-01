from typing import TYPE_CHECKING, Optional, Sequence, Union

from .base import Compiler

if TYPE_CHECKING:
    from sustained.schema import ColumnDef


class PrestoCompiler(Compiler):
    # How each dialect's name is written in prose, for error messages.
    _DISPLAY_NAMES = {"PRESTO": "Presto", "ATHENA": "Athena"}

    _TYPE_MAP = {**Compiler._TYPE_MAP, "BINARY": "VARBINARY"}

    def display_name(self) -> str:
        return self._DISPLAY_NAMES.get(self.dialect_name(), self.dialect_name())

    def quote_identifier(self, identifier: str) -> str:
        # A double quote inside the name doubles, so a name can never end
        # the quoted span early.
        return '"{}"'.format(identifier.replace('"', '""'))

    def parenthesized_set_members(self) -> bool:
        return True

    def validate_column_def(self, column: "ColumnDef") -> None:
        if column.type_name == "ENUM":
            from sustained.exceptions import DialectError

            raise DialectError(
                "Presto has no enum types and cannot enforce a value "
                "list. Use String() and validate values in the "
                "application."
            )

    def supports_constraints(self) -> bool:
        # Presto and Trino query external storage; there are no CHECK or
        # FOREIGN KEY constraints to declare or enforce.
        return False

    def stores_column_comments(self) -> bool:
        return True

    def inline_column_comments(self) -> bool:
        # CREATE TABLE takes the comment inside the column definition.
        return True

    def compile_set_column_comment(
        self,
        table_sql: str,
        column_name: str,
        comment: Optional[str],
        column: Optional["ColumnDef"] = None,
    ) -> "list[str]":
        column_sql = self.quote_identifier(column_name)
        value = "NULL" if comment is None else self.format_value(comment)
        return [f"COMMENT ON COLUMN {table_sql}.{column_sql} IS {value}"]

    def compile_add_check(
        self, table_sql: str, constraint: str, expression: str
    ) -> str:
        from sustained.exceptions import DialectError

        raise DialectError(
            f"{self.display_name()} tables have no CHECK "
            "constraints. Validate rows in the application."
        )

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
        from sustained.exceptions import DialectError

        raise DialectError(
            f"{self.display_name()} tables have no foreign "
            "keys. Enforce the relationship in the application."
        )

    def compile_upsert_statement(
        self,
        table_sql: str,
        column_names: "list[str]",
        row_values_sql: "list[str]",
        conflict_columns: "list[str]",
        action: str,
        update_columns: "list[str]",
    ) -> str:
        from sustained.exceptions import DialectError

        raise DialectError("Presto does not support upserts.")

    def compile_identity(self) -> str:
        from sustained.exceptions import DialectError

        raise DialectError("Presto has no identity columns.")

    def compile_returning(self, columns_sql: str) -> str:
        from sustained.exceptions import DialectError

        raise DialectError("Presto does not support RETURNING clauses.")

    def compile_limit_offset(
        self,
        limit: Optional[int],
        offset: Optional[int],
        has_order_by: bool = False,
    ) -> str:
        # Presto and Trino require OFFSET before LIMIT.
        parts = []
        if offset is not None:
            parts.append(f"OFFSET {offset}")
        if limit is not None:
            parts.append(f"LIMIT {limit}")
        return " ".join(parts)
