"""
Foreign keys to a table in another schema. The catalog reports a key's
target by its bare name, and the reads add the target's schema when it
is not the connection's own. A declared target such as 'app.parents'
compares against both, so a key that already points at the table does
not drop and add on every run, and a restored key points back into the
schema it came from.
"""

import unittest
from unittest import mock

from sustained import create_model
from sustained.autogenerate import autogenerate, diff_schema
from sustained.dialects import Dialects
from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedTable,
    Snapshot,
    introspect_schema,
)
from sustained.schema import ForeignKey, Integer
from tests.test_postgres_introspection import (
    FakeConnection,
    FakeCursor,
    column_row,
    fk_row,
)


def children(target="app.parents", references=None):
    model = create_model("Children", "children")
    model.tableColumns = {
        "id": Integer(primary_key=True),
        "parent_id": Integer(nullable=True, references=references),
    }
    model.columns = ("id", "parent_id")
    if references is None:
        model.tableConstraints = [ForeignKey("fk_parent", "parent_id", f"{target}.id")]
    return model


def snapshot(target_schema="app", key=True):
    foreign_keys = {}
    if key:
        foreign_keys["fk_parent"] = IntrospectedForeignKey(
            ("parent_id",),
            "parents",
            ("id",),
            name="fk_parent",
            target_schema=target_schema,
        )
    return Snapshot(
        tables={
            "children": IntrospectedTable(
                columns={
                    "id": IntrospectedColumn("integer", False, True, name="id"),
                    "parent_id": IntrospectedColumn(
                        "integer", True, False, name="parent_id"
                    ),
                },
                primary_key=("id",),
                foreign_keys=foreign_keys,
                name="children",
            )
        },
        constraints_read=True,
        checks_read=True,
    )


class TestTargetComparison(unittest.TestCase):
    def diff(self, model, schema):
        return diff_schema(None, [model], Dialects.POSTGRES, snapshot=schema)

    def test_a_qualified_target_matches_the_catalog(self):
        diff = self.diff(children(), snapshot())
        self.assertEqual(diff.changed_foreign_keys, [])
        self.assertTrue(diff.is_empty())

    def test_a_qualified_target_in_the_connection_schema_matches(self):
        # The catalog leaves the schema out when it is the connection's.
        diff = self.diff(children(), snapshot(target_schema=None))
        self.assertEqual(diff.changed_foreign_keys, [])

    def test_a_database_in_front_of_the_schema_matches(self):
        diff = self.diff(children("db.app.parents"), snapshot())
        self.assertEqual(diff.changed_foreign_keys, [])

    def test_a_bare_target_differs_from_another_schema(self):
        diff = self.diff(children("parents"), snapshot())
        self.assertEqual(len(diff.changed_foreign_keys), 1)

    def test_another_declared_schema_differs(self):
        diff = self.diff(children("other.parents"), snapshot())
        self.assertEqual(len(diff.changed_foreign_keys), 1)

    def test_the_references_shorthand_compares_the_bare_target(self):
        model = children(references="app.parents.id")
        diff = self.diff(model, snapshot(target_schema=None))
        self.assertEqual(diff.constraint_notes, [])


class TestRestoredKey(unittest.TestCase):
    def test_the_down_step_points_into_the_target_schema(self):
        model = children()
        model.tableConstraints = []
        with mock.patch(
            "sustained.autogenerate.introspect_schema", return_value=snapshot()
        ):
            migration = autogenerate(
                None, [model], id="m", dialect=Dialects.POSTGRES, allow_drops=True
            )
        self.assertEqual(
            migration.up, ['ALTER TABLE "children" DROP CONSTRAINT "fk_parent"']
        )
        self.assertEqual(
            migration.down,
            [
                'ALTER TABLE "children" ADD CONSTRAINT "fk_parent" FOREIGN KEY '
                '("parent_id") REFERENCES "app"."parents" ("id")'
            ],
        )

    def test_a_target_the_snapshot_read_keeps_its_schema(self):
        from sustained.autogenerate import _introspected_fk_sql

        schema = snapshot(target_schema=None)
        schema["parents"] = IntrospectedTable(
            columns={"id": IntrospectedColumn("integer", False, True, name="ID")},
            name="Parents",
            schema="app",
        )
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        fk = schema["children"].foreign_keys["fk_parent"]
        self.assertEqual(
            _introspected_fk_sql(
                compiler, '"children"', "fk_parent", fk, schema, "children"
            ),
            'ALTER TABLE "children" ADD CONSTRAINT "fk_parent" FOREIGN KEY '
            '("parent_id") REFERENCES "app"."Parents" ("ID")',
        )


class TestPostgresReadsTheTargetSchema(unittest.TestCase):
    def read(self, *extra):
        cursor = FakeCursor(
            columns=[column_row("children", "parent_id", "integer")],
            indexes=[],
            foreign_keys=[
                fk_row("fk_parent", "children", "parent_id", "parents", "id") + extra
            ],
        )
        schema = introspect_schema(FakeConnection(cursor), Dialects.POSTGRES)
        self.assertIn("NULLIF(tn.nspname, current_schema())", cursor.statements[2])
        return schema["children"].foreign_keys["fk_parent"]

    def test_a_target_in_another_schema_keeps_it(self):
        self.assertEqual(self.read("app").target_schema, "app")

    def test_a_target_in_the_connection_schema_keeps_none(self):
        self.assertIsNone(self.read(None).target_schema)


class TestSharedReadTakesTheTargetSchema(unittest.TestCase):
    def test_the_eighth_field_is_the_target_schema(self):
        from sustained.introspect import _replace_foreign_keys

        schema = snapshot(key=False)
        row = ("children", "fk_parent", "parent_id", "parents", "id")
        _replace_foreign_keys(
            schema,
            [row + ("NO_ACTION", "CASCADE", "app")],
        )
        fk = schema["children"].foreign_keys["fk_parent"]
        self.assertEqual(fk.target_schema, "app")
        self.assertEqual(fk.on_update, "CASCADE")
        _replace_foreign_keys(schema, [row + ("NO ACTION", "NO ACTION")])
        self.assertIsNone(schema["children"].foreign_keys["fk_parent"].target_schema)

    def test_mysql_and_mssql_ask_for_it(self):
        from sustained.introspect import _schema_plan

        for dialect, expression in (
            (Dialects.MYSQL, "NULLIF(kcu.referenced_table_schema, DATABASE())"),
            (Dialects.MSSQL, "NULLIF(SCHEMA_NAME(rt.schema_id), SCHEMA_NAME())"),
        ):
            plan = _schema_plan(dialect)
            statements = [next(plan)]
            try:
                while True:
                    statements.append(plan.send([]))
            except StopIteration:
                pass
            self.assertTrue(
                any(expression in statement for statement in statements), dialect
            )


if __name__ == "__main__":
    unittest.main()
