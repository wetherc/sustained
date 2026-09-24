"""
Drops and renames on a table outside the connection's schema. A model
that declares tableSchema names its table with the schema in front, and
so does every statement the diff generates for it: a bare name there
names a table in the connection's own schema, which is another table or
none at all.
"""

import unittest
from unittest import mock

from sustained import create_model
from sustained.autogenerate import autogenerate
from sustained.compilers.base import table_qualifier
from sustained.dialects import Dialects
from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
    introspect_schema,
)
from sustained.schema import Integer
from tests.test_postgres_introspection import FakeConnection, FakeCursor, column_row

try:
    import duckdb

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False


def items(**columns):
    model = create_model("AppItems", "items")
    model.tableSchema = "app"
    model.tableColumns = {"id": Integer(primary_key=True), **columns}
    model.columns = tuple(model.tableColumns)
    return model


def snapshot():
    items_table = IntrospectedTable(
        columns={
            "id": IntrospectedColumn("integer", False, True, name="id"),
            "note": IntrospectedColumn("integer", True, False, name="note"),
        },
        primary_key=("id",),
        foreign_keys={
            "fk_note": IntrospectedForeignKey(
                ("note",), "legacy", ("id",), name="fk_note"
            )
        },
        indexes={
            "ix_note": IntrospectedIndex(("note",), False, name="ix_note"),
            "uq_note": IntrospectedIndex(
                ("note",), True, constraint=True, name="uq_note"
            ),
        },
        checks={"ck_note": "((note > 0))"},
        check_names={"ck_note": "ck_note"},
        name="items",
        schema="app",
    )
    legacy = IntrospectedTable(
        columns={"id": IntrospectedColumn("integer", False, True, name="id")},
        primary_key=("id",),
        name="legacy",
        schema="app",
    )
    return Snapshot(
        tables={"items": items_table, "legacy": legacy},
        constraints_read=True,
        checks_read=True,
    )


class TestQualifiedDrops(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch(
            "sustained.autogenerate.introspect_schema",
            side_effect=lambda *_: snapshot(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def generate(self, dialect, model=None, **options):
        return autogenerate(
            None,
            [model or items()],
            id="m",
            dialect=dialect,
            allow_drops=True,
            **options,
        )

    def test_every_drop_names_the_schema_on_postgres(self):
        migration = self.generate(Dialects.POSTGRES)
        self.assertEqual(
            migration.up,
            [
                'ALTER TABLE "app"."items" DROP CONSTRAINT "fk_note"',
                'ALTER TABLE "app"."items" DROP CONSTRAINT "ck_note"',
                'DROP INDEX "app"."ix_note"',
                'ALTER TABLE "app"."items" DROP CONSTRAINT "uq_note"',
                'ALTER TABLE "app"."items" DROP COLUMN "note"',
                'DROP TABLE "app"."legacy"',
            ],
        )

    def test_mysql_names_the_table_in_drop_index(self):
        migration = self.generate(Dialects.MYSQL)
        self.assertIn("DROP INDEX `ix_note` ON `app`.`items`", migration.up)
        self.assertIn("DROP TABLE `app`.`legacy`", migration.up)

    def test_a_column_rename_names_the_schema(self):
        model = items(memo=Integer(nullable=True))
        migration = self.generate(
            Dialects.POSTGRES, model=model, renames={"items.note": "memo"}
        )
        self.assertEqual(
            migration.up[0],
            'ALTER TABLE "app"."items" RENAME COLUMN "note" TO "memo"',
        )

    def test_a_table_rename_names_the_schema(self):
        model = create_model("AppItems", "things")
        model.tableSchema = "app"
        model.tableColumns = {
            "id": Integer(primary_key=True),
            "note": Integer(nullable=True),
        }
        model.columns = ("id", "note")
        migration = autogenerate(
            None,
            [model],
            id="m",
            dialect=Dialects.POSTGRES,
            table_renames={"items": "things"},
            ignore_undeclared=True,
        )
        self.assertEqual(
            migration.up[0], 'ALTER TABLE "app"."items" RENAME TO "things"'
        )
        self.assertEqual(
            migration.down[-1], 'ALTER TABLE "app"."things" RENAME TO "items"'
        )

    def test_mysql_keeps_the_schema_on_the_new_name(self):
        compiler = Dialects.get_compiler(Dialects.MYSQL)
        self.assertEqual(
            compiler.compile_rename_table("`app`.`a`", "`app`.`b`"),
            "ALTER TABLE `app`.`a` RENAME TO `app`.`b`",
        )

    def test_a_rebuilt_index_drops_under_the_schema(self):
        from sustained.schema import Index

        model = items(note=Integer(nullable=True))
        model.indexes = [Index("ix_note", "id", "note")]
        migration = self.generate(Dialects.POSTGRES, model=model)
        self.assertIn('DROP INDEX "app"."ix_note"', migration.up)
        self.assertIn(
            'CREATE INDEX "ix_note" ON "app"."items" ("id", "note")', migration.up
        )


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestDuckdbDropsUnderTheSchema(unittest.TestCase):
    def setUp(self):
        self.conn = duckdb.connect()
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE SCHEMA app")
        self.conn.execute(
            "CREATE TABLE app.items (id INTEGER PRIMARY KEY, note INTEGER)"
        )
        self.conn.execute("CREATE INDEX ix_note ON app.items (note)")
        self.conn.execute("CREATE TABLE app.legacy (id INTEGER)")

    def test_the_drops_run(self):
        migration = autogenerate(
            self.conn,
            [items(note=Integer(nullable=True))],
            id="m",
            dialect=Dialects.DUCKDB,
            allow_drops=True,
        )
        self.assertEqual(
            migration.up, ['DROP INDEX "app"."ix_note"', 'DROP TABLE "app"."legacy"']
        )
        for statement in migration.up:
            self.conn.execute(statement)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE schema_name = 'app'"
            ).fetchone(),
            (1,),
        )


class TestTableQualifier(unittest.TestCase):
    def test_a_bare_name_has_none(self):
        self.assertEqual(table_qualifier('"items"'), "")
        self.assertEqual(table_qualifier("items"), "")

    def test_the_schema_comes_with_its_dot(self):
        self.assertEqual(table_qualifier('"app"."items"'), '"app".')
        self.assertEqual(table_qualifier("[db].[dbo].[items]"), "[db].[dbo].")
        self.assertEqual(table_qualifier("`app`.`items`"), "`app`.")

    def test_a_dot_inside_quotes_is_part_of_the_name(self):
        self.assertEqual(table_qualifier('"a.b"."c.d"'), '"a.b".')
        self.assertEqual(table_qualifier('"a""."."c"'), '"a"".".')
        self.assertEqual(table_qualifier("[a]].b].[c]"), "[a]].b].")


class TestPostgresReadKeepsTheSchema(unittest.TestCase):
    def read(self, *schemas):
        cursor = FakeCursor(
            columns=[
                column_row("items", "id", "integer", schema="app"),
                column_row("plain", "id", "integer", schema="public"),
            ],
            indexes=[],
            foreign_keys=[],
        )
        return introspect_schema(FakeConnection(cursor), Dialects.POSTGRES, schemas)

    def test_a_declared_schema_is_kept(self):
        schema = self.read("App")
        self.assertEqual(schema["items"].schema, "app")
        self.assertIsNone(schema["plain"].schema)

    def test_no_declared_schema_keeps_none(self):
        self.assertIsNone(self.read()["items"].schema)


if __name__ == "__main__":
    unittest.main()
