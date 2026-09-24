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
from sustained.schema import Check, Enum, Integer, String

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

    def test_an_added_enum_value_is_accepted(self):
        # Postgres appends to its type and MySQL restates the value list.
        # SQLite rebuilds the table with the new check and SQL Server
        # replaces the check, where the added value used to read as no
        # change. DuckDB cannot append to a type and refuses. The new
        # value is no longer than the others, since the SQL Server read
        # does not report a VARCHAR length to widen.
        mood = Enum("sad", "ok", name="it_mood")
        migrator = self.migrator()
        migrator.up(models=[self.changed_model(mood=mood)])
        wider = self.changed_model(mood=Enum("sad", "ok", "meh", name="it_mood"))
        if self.DIALECT == Dialects.DUCKDB:
            with self.assertRaises(Exception):
                migrator.up(models=[wider], unrehearsed=True)
            return
        wider.bind(self.connection)
        try:
            migrator.up(models=[wider], unrehearsed=True)
            self.assertIsNone(migrator.plan([wider]))
            wider.query().insert([{**ROW, "mood": "meh"}]).run()
        finally:
            wider.unbind()

    def test_a_foreign_key_reads_its_target_and_its_action(self):
        # MySQL, MariaDB, and SQL Server reported every key's target as
        # unknown, so a changed action never diffed.
        from sustained.schema import ForeignKey

        maker = type(
            "MakerTarget",
            (Model,),
            {
                "tableName": "it_makers",
                "tableColumns": {"id": Integer(primary_key=True)},
                "_dialect": self.DIALECT,
            },
        )

        def widget(on_delete=None):
            return type(
                "WidgetKeyed",
                (Model,),
                {
                    "tableName": "it_widgets",
                    "tableColumns": {
                        "id": Integer(primary_key=True),
                        "maker_id": Integer(),
                    },
                    "tableConstraints": [
                        ForeignKey(
                            "fk_it_widgets_maker",
                            "maker_id",
                            "it_makers.id",
                            on_delete=on_delete,
                        )
                    ],
                    "_dialect": self.DIALECT,
                },
            )

        migrator = self.migrator()
        migrator.up(models=[maker, widget()])
        self.assertIsNone(migrator.plan([maker, widget()]))
        key = self.tables()["it_widgets"].foreign_keys
        (fk,) = key.values()
        self.assertEqual("it_makers", fk.target_table)
        if not self.constraints_fixed():
            changed = migrator.plan([maker, widget("CASCADE")], allow_drops=True)
            self.assertIsNotNone(changed)

    def test_two_tables_keep_their_own_same_named_check(self):
        # A check name is unique per table on Postgres and SQLite. The
        # Postgres read joined checks on the schema and the name, so one
        # table could read the other table's expression.
        if self.DIALECT not in (Dialects.POSTGRES, Dialects.DEFAULT):
            self.skipTest("the engine keeps check names unique per schema")

        def checked(class_name, table, expression):
            return type(
                class_name,
                (Model,),
                {
                    "tableName": table,
                    "tableColumns": {
                        "id": Integer(primary_key=True),
                        "size": Integer(),
                    },
                    "tableConstraints": [Check("ck_it_bounds", expression)],
                    "_dialect": self.DIALECT,
                },
            )

        models = [
            checked("WidgetBounded", "it_widgets", "size > 0"),
            checked("MakerBounded", "it_makers", "size < 100"),
        ]
        migrator = self.migrator()
        migrator.up(models=models)
        self.assertIsNone(migrator.plan(models))
        tables = self.tables()
        self.assertIn(">", tables["it_widgets"].checks["ck_it_bounds"])
        self.assertIn("<", tables["it_makers"].checks["ck_it_bounds"])

    def test_tables_no_model_declares_drop_child_first(self):
        # The catalog lists it_makers before it_widgets, and the engine
        # refuses to drop a table that a foreign key still names.
        from sustained.schema import ForeignKey

        def model(class_name, table, **extra):
            return type(
                class_name,
                (Model,),
                {"tableName": table, "_dialect": self.DIALECT, **extra},
            )

        maker = model(
            "MakerDropped", "it_makers", tableColumns={"id": Integer(primary_key=True)}
        )
        widget = model(
            "WidgetDropped",
            "it_widgets",
            tableColumns={"id": Integer(primary_key=True), "maker_id": Integer()},
            tableConstraints=[
                ForeignKey("fk_it_widgets_maker", "maker_id", "it_makers.id")
            ],
        )
        events = model(
            "EventsKept", "it_events", tableColumns={"id": Integer(primary_key=True)}
        )
        migrator = self.migrator()
        migrator.up(models=[maker, widget])
        self.execute("INSERT INTO it_makers (id) VALUES (1)")
        self.execute("INSERT INTO it_widgets (id, maker_id) VALUES (1, 1)")
        migrator.up(models=[events], allow_drops=True, unrehearsed=True)
        self.assertIsNone(migrator.plan([events], allow_drops=True))

    def test_mixed_case_names_drop_as_spelled(self):
        # A read keys every name in lower case. Postgres takes a quoted
        # name as written, and MySQL on Linux does the same for a table,
        # so a drop that named the key named nothing there.
        compiler = Dialects.get_compiler(self.DIALECT)
        quote = compiler.quote_identifier
        migrator = self.migrator()
        migrator.up(models=[self.Widget])
        self.execute(f"CREATE TABLE {quote('It_Legacy')} ({quote('Id')} INTEGER)")
        self.execute(
            compiler.compile_add_column(
                quote("it_widgets"), f"{quote('Old_Note')} INTEGER"
            )
        )
        self.execute(
            f"CREATE INDEX {quote('IX_It_Size')} ON {quote('it_widgets')} "
            f"({quote('size')})"
        )
        migrator.up(models=[self.Widget], allow_drops=True, unrehearsed=True)
        self.assertIsNone(migrator.plan([self.Widget], allow_drops=True))
        self.assertNotIn("it_legacy", self.tables())
