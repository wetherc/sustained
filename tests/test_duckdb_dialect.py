"""
Tests for the DuckDB dialect.
"""

import unittest

from sustained import create_model
from sustained.dialects import Dialects

try:
    import duckdb

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False

Duck = create_model("DuckModel", "events")
Duck.set_dialect(Dialects.DUCKDB)


class TestDuckDbRendering(unittest.TestCase):
    def test_identifier_quoting(self):
        sql = str(Duck.query().select("events.kind"))
        self.assertEqual(sql, 'SELECT "events"."kind" FROM "events"')

    def test_native_ilike(self):
        sql = str(Duck.query().whereILike("kind", "a%"))
        self.assertIn("\"kind\" ILIKE 'a%'", sql)

    def test_qmark_placeholders(self):
        sql, params = Duck.query().where("id", "=", 1).to_sql()
        self.assertIn("= ?", sql)
        self.assertEqual(params, (1,))

    def test_upsert_uses_on_conflict(self):
        sql = str(Duck.query().insert({"id": 1, "kind": "x"}).onConflict("id").merge())
        self.assertIn("ON CONFLICT", sql)

    def test_registered_functions(self):
        self.assertIn("NOW()", str(Duck.query().now(alias="ts")))


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestDuckDbExecution(unittest.TestCase):
    def test_round_trip(self):
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE events (id INTEGER, kind TEXT)")
        Duck.bind(conn)
        try:
            Duck.query().insert([{"id": 1, "kind": "a"}, {"id": 2, "kind": "b"}]).run()
            rows = Duck.query().orderBy("id").to_dicts()
            self.assertEqual(rows[0]["kind"], "a")
        finally:
            Duck.unbind()
            conn.close()


if __name__ == "__main__":
    unittest.main()
