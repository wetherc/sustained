"""
A join that returns the same column name twice is refused at hydration,
sync and async, instead of keeping the last value for that name.
"""

import sqlite3
import unittest

from sustained import Model
from sustained.aio import DbApiAsyncAdapter
from sustained.exceptions import AmbiguousColumns
from sustained.execution import checked_columns
from sustained.schema import Integer, String

SCHEMA = [
    "CREATE TABLE amb_users (id INTEGER PRIMARY KEY, name TEXT)",
    "CREATE TABLE amb_accounts (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT)",
    "INSERT INTO amb_users (id, name) VALUES (1, 'Ada')",
    "INSERT INTO amb_accounts (id, user_id, name) VALUES (7, 1, 'primary')",
]


class AmbUser(Model):
    tableName = "amb_users"
    tableColumns = {"id": Integer(primary_key=True), "name": String(50)}


def joined_query():
    """A join whose select list carries id and name from both tables."""
    return (
        AmbUser.query()
        .select(
            "amb_users.id", "amb_users.name", "amb_accounts.id", "amb_accounts.name"
        )
        .join("amb_accounts", "amb_users.id", "=", "amb_accounts.user_id")
    )


class TestCheckedColumns(unittest.TestCase):
    def test_unique_names_pass_through(self):
        self.assertEqual(checked_columns(["id", "name"]), ["id", "name"])

    def test_repeated_name_is_named(self):
        with self.assertRaises(AmbiguousColumns) as caught:
            checked_columns(["id", "name", "id"])
        self.assertEqual(caught.exception.columns, ["id"])
        self.assertIn("'id'", str(caught.exception))

    def test_every_repeat_is_reported_once(self):
        with self.assertRaises(AmbiguousColumns) as caught:
            checked_columns(["id", "name", "id", "name", "name"])
        self.assertEqual(caught.exception.columns, ["id", "name"])


class TestSyncHydration(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        for statement in SCHEMA:
            self.conn.execute(statement)
        AmbUser.bind(self.conn)

    def tearDown(self):
        AmbUser.unbind()
        self.conn.close()

    def test_run_refuses_the_join(self):
        with self.assertRaises(AmbiguousColumns):
            joined_query().run()

    def test_to_dicts_refuses_the_join(self):
        with self.assertRaises(AmbiguousColumns):
            joined_query().to_dicts()

    def test_aliased_join_still_runs(self):
        rows = (
            AmbUser.query()
            .select("amb_users.id", "amb_accounts.id AS account_id")
            .join("amb_accounts", "amb_users.id", "=", "amb_accounts.user_id")
            .to_dicts()
        )
        self.assertEqual(rows, [{"id": 1, "account_id": 7}])


class TestAsyncHydration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        for statement in SCHEMA:
            self.conn.execute(statement)
        self.adapter = DbApiAsyncAdapter(self.conn)
        AmbUser.bind_async(self.adapter)

    def tearDown(self):
        AmbUser.unbind_async()
        self.conn.close()

    async def test_arun_refuses_the_join(self):
        with self.assertRaises(AmbiguousColumns):
            await joined_query().arun()

    async def test_ato_dicts_refuses_the_join(self):
        with self.assertRaises(AmbiguousColumns):
            await joined_query().ato_dicts()


if __name__ == "__main__":
    unittest.main()
