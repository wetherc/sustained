"""
Schema changes a generated migration makes to a table that exists, part
of the `migrations` cover. Each test applies the models, changes one
declaration, applies them again, and reads the schema back: the plan
for the changed models must then be empty.

ServerCase in lifecycle.py mixes this in, so every server that claims
the `migrations` cover runs these tests.
"""

from sustained.dialects import Dialects
from sustained.model import Model
from sustained.schema import String

from .column_types import ROW, typed_columns


class SchemaChangeTests:
    """Mixed into lifecycle.ServerCase; uses its helpers and fixtures."""

    def changed_model(self, **overrides):
        return type(
            "WidgetChanged",
            (Model,),
            {
                "tableName": "it_widgets",
                "tableColumns": {**typed_columns(), **overrides},
                "_dialect": self.DIALECT,
            },
        )

    def constraints_fixed(self):
        compiler = Dialects.get_compiler(self.DIALECT)
        return compiler.supports_alter_column() and not (
            compiler.supports_add_constraint()
        )

    def test_a_column_loses_its_unique_constraint(self):
        # Postgres and SQL Server refuse DROP INDEX on the index behind a
        # UNIQUE constraint, SQLite rebuilds the table to lose it, and
        # DuckDB cannot drop a constraint, so there it stays a note.
        migrator = self.migrator()
        migrator.up(models=[self.changed_model()])
        loose = self.changed_model(name=String(80, nullable=False))
        if self.constraints_fixed():
            self.assertIsNone(migrator.plan([loose], allow_drops=True))
            return
        loose.bind(self.connection)
        try:
            migrator.up(models=[loose], allow_drops=True, unrehearsed=True)
            self.assertIsNone(migrator.plan([loose], allow_drops=True))
            loose.query().insert([ROW, {**ROW, "id": 2}]).run()
        finally:
            loose.unbind()
