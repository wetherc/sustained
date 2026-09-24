"""
Taking unique=True off a column. The index behind a UNIQUE constraint
belongs to the constraint, so each engine removes it its own way:
DROP CONSTRAINT where the engine has one, a rebuild on SQLite, and a
note on DuckDB, which cannot drop a constraint at all.
"""

import sqlite3
import unittest
from unittest import mock

from sustained import create_model
from sustained.autogenerate import autogenerate, diff_schema
from sustained.dialects import Dialects
from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
)
from sustained.schema import Integer, String, Text


def members(email_unique=False, **extra):
    model = create_model("UniqueMember", "members")
    model.tableColumns = {
        "id": Integer(primary_key=True),
        "email": String(80, unique=email_unique),
        **extra,
    }
    model.columns = tuple(model.tableColumns)
    return model


class TestSqliteUniqueRemoval(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE members (id INTEGER PRIMARY KEY, email VARCHAR(80) UNIQUE)"
        )
        self.conn.execute("INSERT INTO members VALUES (1, 'a@x')")

    def test_the_diff_reports_the_constraint(self):
        diff = diff_schema(self.conn, [members()])
        ((table, name, index),) = diff.extra_indexes
        self.assertEqual(
            (table, index.columns, index.constraint), ("members", ("email",), True)
        )
        self.assertIn(f"drop unique constraint {name} on members", diff.summary())
        self.assertTrue(diff_schema(self.conn, [members(True)]).is_empty())

    def test_generation_needs_allow_drops(self):
        with self.assertRaisesRegex(ValueError, "sqlite_autoindex_members_1"):
            autogenerate(self.conn, [members()], id="m")

    def test_allow_drops_rebuilds_the_table_without_it(self):
        migration = autogenerate(self.conn, [members()], id="m", allow_drops=True)
        self.assertFalse(any("DROP INDEX" in s for s in migration.up))
        for statement in migration.up:
            self.conn.execute(statement)
        self.conn.execute("INSERT INTO members VALUES (2, 'a@x')")
        self.assertTrue(diff_schema(self.conn, [members()]).is_empty())

    def test_a_rebuild_without_drops_keeps_it(self):
        model = members(note=Text(nullable=False, default=""))
        migration = autogenerate(self.conn, [model], id="m", ignore_undeclared=True)
        for statement in migration.up:
            self.conn.execute(statement)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO members (id, email) VALUES (2, 'a@x')")


def snapshot(constraint):
    return Snapshot(
        tables={
            "members": IntrospectedTable(
                columns={
                    "id": IntrospectedColumn("integer", False, True),
                    "email": IntrospectedColumn("varchar(80)", True, False),
                },
                primary_key=("id",),
                indexes={
                    "members_email_key": IntrospectedIndex(
                        ("email",), True, constraint=constraint
                    )
                },
            )
        },
        constraints_read=True,
    )


class TestUniqueRemovalOnAlterDialects(unittest.TestCase):
    def generate(self, dialect, constraint=True):
        with mock.patch(
            "sustained.autogenerate.introspect_schema",
            return_value=snapshot(constraint),
        ):
            return autogenerate(
                None, [members()], id="m", dialect=dialect, allow_drops=True
            )

    def test_postgres_drops_the_constraint(self):
        migration = self.generate(Dialects.POSTGRES)
        self.assertEqual(
            migration.up,
            ['ALTER TABLE "members" DROP CONSTRAINT "members_email_key"'],
        )
        self.assertEqual(
            migration.down,
            [
                'ALTER TABLE "members" ADD CONSTRAINT "members_email_key" '
                'UNIQUE ("email")'
            ],
        )

    def test_mssql_drops_the_constraint(self):
        migration = self.generate(Dialects.MSSQL)
        self.assertEqual(
            migration.up, ["ALTER TABLE [members] DROP CONSTRAINT [members_email_key]"]
        )

    def test_a_plain_unique_index_still_takes_drop_index(self):
        migration = self.generate(Dialects.POSTGRES, constraint=False)
        self.assertEqual(migration.up, ['DROP INDEX "members_email_key"'])

    def test_duckdb_reports_a_note(self):
        with mock.patch(
            "sustained.autogenerate.introspect_schema", return_value=snapshot(True)
        ):
            diff = diff_schema(None, [members()], dialect=Dialects.DUCKDB)
        self.assertEqual(diff.extra_indexes, [])
        (note,) = diff.constraint_notes
        self.assertIn("unique constraint 'members_email_key'", note)


if __name__ == "__main__":
    unittest.main()
