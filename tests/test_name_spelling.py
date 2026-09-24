"""
Names the catalog spells in mixed case. A snapshot keys every name in
lower case, and Postgres takes a quoted name as written, so a statement
that names a table, column, index, or constraint renders the spelling
the catalog reported.
"""

import unittest
from unittest import mock

from sustained import create_model
from sustained.autogenerate import autogenerate, diff_schema
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


def items():
    model = create_model("SpelledItems", "items")
    model.tableColumns = {"id": Integer(primary_key=True)}
    model.columns = ("id",)
    return model


def snapshot():
    owners = IntrospectedTable(
        columns={
            "ownerkey": IntrospectedColumn("integer", False, True, name="OwnerKey")
        },
        primary_key=("ownerkey",),
        name="Owners",
    )
    items_table = IntrospectedTable(
        columns={
            "id": IntrospectedColumn("integer", False, True, name="id"),
            "oldnote": IntrospectedColumn("integer", True, False, name="OldNote"),
            "ownerref": IntrospectedColumn("integer", True, False, name="OwnerRef"),
        },
        primary_key=("id",),
        foreign_keys={
            "fk_owner": IntrospectedForeignKey(
                ("ownerref",), "owners", ("ownerkey",), name="FK_Owner"
            )
        },
        indexes={
            "ix_note": IntrospectedIndex(("oldnote",), False, name="IX_Note"),
            "uq_note": IntrospectedIndex(
                ("oldnote",), True, constraint=True, name="UQ_Note"
            ),
        },
        checks={"ck_pos": "((oldnote > 0))"},
        check_names={"ck_pos": "CK_Pos"},
        name="items",
    )
    return Snapshot(
        tables={
            "items": items_table,
            "owners": owners,
            "legacy": owners._replace(name="Legacy"),
        },
        constraints_read=True,
        checks_read=True,
    )


class TestSpelledDrops(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch(
            "sustained.autogenerate.introspect_schema",
            side_effect=lambda *_: snapshot(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_diff_reports_the_spelling(self):
        diff = diff_schema(None, [items()], dialect=Dialects.POSTGRES)
        self.assertEqual(sorted(diff.extra_tables), ["Legacy", "Owners"])
        self.assertEqual(
            sorted(diff.extra_columns), [("items", "OldNote"), ("items", "OwnerRef")]
        )
        self.assertEqual(
            sorted(name for _, name, _ in diff.extra_indexes), ["IX_Note", "UQ_Note"]
        )
        self.assertEqual([n for _, n, _ in diff.extra_foreign_keys], ["FK_Owner"])
        self.assertEqual([n for _, n, _ in diff.extra_checks], ["CK_Pos"])

    def test_every_drop_names_the_spelling(self):
        migration = autogenerate(
            None, [items()], id="m", dialect=Dialects.POSTGRES, allow_drops=True
        )
        self.assertEqual(
            migration.up,
            [
                'ALTER TABLE "items" DROP CONSTRAINT "FK_Owner"',
                'ALTER TABLE "items" DROP CONSTRAINT "CK_Pos"',
                'DROP INDEX "IX_Note"',
                'ALTER TABLE "items" DROP CONSTRAINT "UQ_Note"',
                'ALTER TABLE "items" DROP COLUMN "OldNote"',
                'ALTER TABLE "items" DROP COLUMN "OwnerRef"',
                'DROP TABLE "Owners"',
                'DROP TABLE "Legacy"',
            ],
        )

    def test_a_restored_key_names_the_spelling(self):
        from sustained.autogenerate import _introspected_fk_sql

        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        fk = snapshot()["items"].foreign_keys["fk_owner"]
        self.assertEqual(
            _introspected_fk_sql(
                compiler, '"items"', "FK_Owner", fk, snapshot(), "items"
            ),
            'ALTER TABLE "items" ADD CONSTRAINT "FK_Owner" FOREIGN KEY '
            '("OwnerRef") REFERENCES "Owners" ("OwnerKey")',
        )


class TestPostgresReadKeepsTheSpelling(unittest.TestCase):
    def test_names_come_back_as_spelled(self):
        cursor = FakeCursor(
            columns=[column_row("Legacy", "OldNote", "integer")],
            indexes=[("Legacy", "IX_Note", False, False, "OldNote")],
            foreign_keys=[],
            checks=[("Legacy", "CK_Pos", "((OldNote > 0))")],
        )
        table = introspect_schema(FakeConnection(cursor), Dialects.POSTGRES)["legacy"]
        self.assertEqual(table.name, "Legacy")
        self.assertEqual(table.spelled_column("oldnote"), "OldNote")
        self.assertEqual(table.indexes["ix_note"].name, "IX_Note")
        self.assertEqual(table.check_names, {"ck_pos": "CK_Pos"})
        self.assertEqual(table.spelled_column("missing"), "missing")


if __name__ == "__main__":
    unittest.main()
