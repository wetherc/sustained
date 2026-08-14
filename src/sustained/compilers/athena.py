"""
The AWS Athena dialect.

Athena runs a Trino-based engine over files in S3, so it inherits the
Presto compiler's query behavior and replaces everything that touches
storage. Tables have no constraints, no indexes, and no identity columns.
DDL takes Athena's spellings: STRING instead of TEXT, ADD COLUMNS instead
of ADD COLUMN, CHANGE COLUMN for type changes, and a PARTITIONED BY,
LOCATION, and TBLPROPERTIES clause after the column list. Placeholders
render as %s to match pyathena, the DB-API driver for Athena.

Upserts, UPDATE, DELETE, and in-place column changes only work on Iceberg
tables (created with the table_type=ICEBERG property).
"""

from typing import Any, Optional

from sustained.exceptions import DialectError

from .presto import PrestoCompiler


class AthenaCompiler(PrestoCompiler):
    _TYPE_MAP = {
        "INTEGER": "INT",
        "BIGINT": "BIGINT",
        "VARCHAR": "VARCHAR",
        "TEXT": "STRING",
        "BOOLEAN": "BOOLEAN",
        "FLOAT": "DOUBLE",
        "NUMERIC": "DECIMAL",
        "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP",
        "JSON": "STRING",
    }

    def placeholder(self) -> str:
        # pyathena uses the pyformat parameter style.
        return "%s"

    def compile_column_type(self, column: Any) -> str:
        # Athena DDL has no unbounded VARCHAR; STRING is the unbounded form.
        if column.type_name == "VARCHAR" and column.length is None:
            return "STRING"
        return super().compile_column_type(column)

    def validate_column_def(self, column: Any) -> None:
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

    def compile_identity(self) -> str:
        raise DialectError("Athena has no identity columns.")

    def compile_table_options(self, options: Any) -> str:
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
                f"'{k}'='{str(v)}'" for k, v in options.properties.items()
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
        columns_sql = ", ".join(self.quote_identifier(c) for c in column_names)
        on_sql = " AND ".join(
            f"target.{self.quote_identifier(c)} = source.{self.quote_identifier(c)}"
            for c in conflict_columns
        )
        sql = (
            f"MERGE INTO {table_sql} AS target "
            f"USING (VALUES {', '.join(row_values_sql)}) AS source ({columns_sql}) "
            f"ON {on_sql}"
        )
        if action == "merge":
            assignments = ", ".join(
                f"{self.quote_identifier(c)} = source.{self.quote_identifier(c)}"
                for c in update_columns
            )
            sql += f" WHEN MATCHED THEN UPDATE SET {assignments}"
        insert_values = ", ".join(
            f"source.{self.quote_identifier(c)}" for c in column_names
        )
        sql += (
            f" WHEN NOT MATCHED THEN INSERT ({columns_sql}) "
            f"VALUES ({insert_values})"
        )
        return sql

    def compile_returning(self, columns_sql: str) -> str:
        raise DialectError("Athena does not support RETURNING clauses.")

    def compile_ctas(self, table_sql: str, select_sql: str, temporary: bool) -> str:
        if temporary:
            raise DialectError("Athena has no temporary tables.")
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
        raise DialectError("Athena cannot rename tables.")

    def compile_create_index(
        self,
        index_name: str,
        table_sql: str,
        columns: "list[str]",
        unique: bool,
    ) -> str:
        raise DialectError(
            "Athena has no indexes. Remove the model's indexes declaration; "
            "use partitioning through table options instead."
        )

    def compile_drop_index(self, index_name: str, table_sql: str) -> str:
        raise DialectError("Athena has no indexes.")

    def supports_alter_column(self) -> bool:
        return True

    def supports_constraints(self) -> bool:
        return False

    def supports_transactions(self) -> bool:
        return False

    def compile_alter_column_type(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        using: Optional[str] = None,
    ) -> "list[str]":
        # Iceberg tables allow widening type changes: int to bigint, float
        # to double, and growing a decimal's precision.
        if using is not None:
            raise DialectError(
                "Athena cannot cast values while changing a column type. "
                "Remove the type_casts hint."
            )
        quoted = self.quote_identifier(column_name)
        return [f"ALTER TABLE {table_sql} CHANGE COLUMN {quoted} {quoted} {type_sql}"]

    def compile_alter_column_nullability(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        nullable: bool,
    ) -> "list[str]":
        raise DialectError(
            "Athena columns are always nullable; nullability cannot change."
        )
