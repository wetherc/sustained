"""
Enum value changes on the dialects where an enum column is a VARCHAR
held to its values by a CHECK constraint: SQLite, where the table is
rebuilt, and SQL Server, where the check is dropped and added again.
"""

import sqlite3
import unittest
from unittest import mock

from sustained import create_model
from sustained.autogenerate import autogenerate, diff_schema
from sustained.dialects import Dialects
from sustained.introspect import IntrospectedColumn, IntrospectedTable, Snapshot
from sustained.schema import Enum, Integer


def posts(*values):
    model = create_model("CheckedPost", "posts")
    model.tableColumns = {
        "id": Integer(primary_key=True),
        "status": Enum(*values, name="post_status"),
    }
    model.columns = tuple(model.tableColumns)
    return model


class TestSqliteEnumChecks(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        for statement in autogenerate(self.conn, [posts("draft", "live")], id="a").up:
            self.conn.execute(statement)
        self.conn.execute("INSERT INTO posts VALUES (1, 'live')")

    def apply(self, model):
        for statement in autogenerate(self.conn, [model], id="b").up:
            self.conn.execute(statement)

    def test_a_matching_check_diffs_clean(self):
        self.assertTrue(diff_schema(self.conn, [posts("live", "draft")]).is_empty())

    def test_an_added_value_is_reported(self):
        diff = diff_schema(self.conn, [posts("draft", "live", "gone")])
        ((_, name, live, _),) = diff.changed_enum_checks
        self.assertEqual((name, live), ("status", ("draft", "live")))
        self.assertIn(
            "change the values of enum column posts.status: database permits "
            "(draft, live), model declares (draft, live, gone)",
            diff.summary(),
        )
        self.assertEqual(
            diff.outstanding(),
            [
                "enum column 'posts.status' permits (draft, live), the models "
                "declare (draft, live, gone)"
            ],
        )

    def test_the_rebuild_takes_the_new_value_and_converges(self):
        model = posts("draft", "live", "gone")
        self.apply(model)
        self.conn.execute("INSERT INTO posts VALUES (2, 'gone')")
        self.assertTrue(diff_schema(self.conn, [model]).is_empty())
        (sql,) = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'posts'"
        ).fetchone()
        self.assertIn('"ck_posts_status_enum"', sql)

    def test_a_removed_value_in_use_fails_the_copy(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply(posts("draft"))


def mssql_snapshot(check, status_type="nvarchar(5)"):
    return Snapshot(
        tables={
            "posts": IntrospectedTable(
                columns={
                    "id": IntrospectedColumn("int", False, True),
                    "status": IntrospectedColumn(status_type, True, False),
                },
                primary_key=("id",),
                checks={} if check is None else {"ck_posts_status_enum": check},
            )
        },
        constraints_read=True,
        checks_read=True,
    )


READ_BACK = "([status]=N'draft' OR [status]=N'live')"
DROP = "ALTER TABLE [posts] DROP CONSTRAINT [ck_posts_status_enum]"


class TestMssqlEnumChecks(unittest.TestCase):
    def generate(self, model, check=READ_BACK, **kwargs):
        with mock.patch(
            "sustained.autogenerate.introspect_schema",
            return_value=mssql_snapshot(check, **kwargs),
        ):
            return autogenerate(None, [model], id="m", dialect=Dialects.MSSQL)

    def test_the_read_back_check_matches_its_declaration(self):
        self.assertIsNone(self.generate(posts("live", "draft")))

    def test_an_added_value_replaces_the_check(self):
        migration = self.generate(posts("draft", "live", "gone"))
        added = (
            "ALTER TABLE [posts] ADD CONSTRAINT [ck_posts_status_enum] "
            "CHECK ([status] IN (N'draft', N'live', N'gone'))"
        )
        restored = (
            "ALTER TABLE [posts] ADD CONSTRAINT [ck_posts_status_enum] "
            f"CHECK ({READ_BACK})"
        )
        self.assertEqual(migration.up, [DROP, added])
        self.assertEqual(migration.down, [DROP, restored])

    def test_a_longer_value_widens_the_column_between_drop_and_add(self):
        migration = self.generate(posts("draft", "live", "archived"))
        self.assertEqual(migration.up[0], DROP)
        self.assertIn("ALTER COLUMN [status] NVARCHAR(8)", migration.up[1])
        self.assertIn("ADD CONSTRAINT [ck_posts_status_enum]", migration.up[2])
        self.assertEqual(migration.down[0], DROP)
        self.assertIn("ALTER COLUMN [status] nvarchar(5)", migration.down[1])
        self.assertIn(READ_BACK, migration.down[2])

    def test_a_missing_check_is_added(self):
        migration = self.generate(posts("draft", "live"), check=None)
        self.assertEqual(len(migration.up), 1)
        self.assertIn("ADD CONSTRAINT [ck_posts_status_enum]", migration.up[0])
        self.assertEqual(migration.down, [DROP])


if __name__ == "__main__":
    unittest.main()
