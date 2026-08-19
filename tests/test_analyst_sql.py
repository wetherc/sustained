"""
Tests for recursive CTEs, set operations, DISTINCT ON, grouping modes,
QUALIFY, row locking, total(), cursor pagination, and explain().
"""

import sqlite3
import unittest

from sustained import DialectError, Model, create_model
from sustained.dialects import Dialects

User = create_model("AnUser", "users")
Pg = create_model("AnPg", "pt")
Pg.set_dialect(Dialects.POSTGRES)
Duck = create_model("AnDuck", "dt")
Duck.set_dialect(Dialects.DUCKDB)


class TestRecursiveCtes(unittest.TestCase):
    def test_recursive_keyword(self):
        base = User.query().select("id")
        sql = str(User.query().with_("tree", base, recursive=True).from_("tree"))
        self.assertTrue(sql.startswith("WITH RECURSIVE tree AS ("))

    def test_plain_with_unchanged(self):
        base = User.query().select("id")
        sql = str(User.query().with_("c", base))
        self.assertTrue(sql.startswith("WITH c AS ("))

    def test_mssql_omits_recursive_keyword(self):
        Ms = create_model("AnMs", "t")
        Ms.set_dialect(Dialects.MSSQL)
        base = Ms.query().select("id")
        sql = str(Ms.query().with_("tree", base, recursive=True).from_("tree"))
        self.assertTrue(sql.startswith("WITH tree AS ("))


class TestSetOperations(unittest.TestCase):
    def test_intersect(self):
        sql = str(User.query().select("id").intersect(User.query().select("id")))
        self.assertIn(" INTERSECT SELECT ", sql)

    def test_except(self):
        sql = str(User.query().select("id").except_(User.query().select("id")))
        self.assertIn(" EXCEPT SELECT ", sql)

    def test_default_members_render_bare(self):
        # SQLite rejects a parenthesized set-operation member, so the
        # default dialect renders members without parentheses.
        sql = str(User.query().select("id").union(User.query().select("id")))
        self.assertEqual("SELECT id FROM users UNION SELECT id FROM users", sql)

    def test_parenthesizing_dialects_keep_member_clauses(self):
        capped = Pg.query().select("id").orderBy("id").limit(1)
        sql = str(Pg.query().select("id").union(capped))
        self.assertEqual(
            '(SELECT "id" FROM "pt") UNION '
            '(SELECT "id" FROM "pt" ORDER BY "id" ASC LIMIT 1)',
            sql,
        )

    def test_a_bare_member_refuses_its_own_order_and_limit(self):
        capped = User.query().select("id").orderBy("id").limit(1)
        with self.assertRaises(DialectError) as raised:
            str(User.query().select("id").union(capped))
        self.assertIn("without parentheses", str(raised.exception))


class TestDistinctOn(unittest.TestCase):
    def test_postgres_renders(self):
        sql = str(Pg.query().distinctOn("pt.user_id").select("*"))
        self.assertIn('SELECT DISTINCT ON ("pt"."user_id") *', sql)

    def test_duckdb_renders(self):
        sql = str(Duck.query().distinctOn("k").select("*"))
        self.assertIn('DISTINCT ON ("k")', sql)

    def test_default_dialect_raises(self):
        with self.assertRaises(DialectError):
            str(User.query().distinctOn("a"))

    def test_exclusive_with_distinct(self):
        with self.assertRaises(ValueError):
            User.query().distinct().distinctOn("a")

    def test_requires_columns(self):
        with self.assertRaises(ValueError):
            User.query().distinctOn()


class TestGroupingModes(unittest.TestCase):
    def test_rollup(self):
        sql = str(User.query().count().groupByRollup("region", "city"))
        self.assertTrue(sql.endswith("GROUP BY ROLLUP (region, city)"))

    def test_cube(self):
        sql = str(User.query().count().groupByCube("a", "b"))
        self.assertTrue(sql.endswith("GROUP BY CUBE (a, b)"))

    def test_grouping_sets(self):
        sql = str(User.query().count().groupByGroupingSets(("a", "b"), ("a",), ()))
        self.assertTrue(sql.endswith("GROUP BY GROUPING SETS ((a, b), (a), ())"))

    def test_rollup_requires_columns(self):
        with self.assertRaises(ValueError):
            User.query().groupByRollup()

    def test_grouping_sets_require_sets(self):
        with self.assertRaises(ValueError):
            User.query().groupByGroupingSets()


class TestQualify(unittest.TestCase):
    def test_duckdb_renders(self):
        sql = str(
            Duck.query()
            .select_window("ROW_NUMBER", "rn", partition_by=["k"], order_by=["ts"])
            .qualify("rn = 1")
        )
        self.assertTrue(sql.endswith("QUALIFY rn = 1"))

    def test_qualify_with_predicate(self):
        from sustained import col

        sql = str(Duck.query().select("*").qualify(col("rn") == 1))
        self.assertTrue(sql.endswith('QUALIFY "rn" = 1'))

    def test_other_dialects_raise(self):
        with self.assertRaises(DialectError):
            str(User.query().qualify("rn = 1"))


class TestForUpdate(unittest.TestCase):
    def test_postgres_variants(self):
        base = 'SELECT * FROM "pt" WHERE "id" = 1'
        self.assertEqual(
            str(Pg.query().where("id", "=", 1).for_update()), f"{base} FOR UPDATE"
        )
        self.assertTrue(
            str(Pg.query().where("id", "=", 1).for_update(skip_locked=True)).endswith(
                "FOR UPDATE SKIP LOCKED"
            )
        )
        self.assertTrue(
            str(Pg.query().where("id", "=", 1).for_update(nowait=True)).endswith(
                "FOR UPDATE NOWAIT"
            )
        )

    def test_flags_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            Pg.query().for_update(skip_locked=True, nowait=True)

    def test_unsupported_dialect_raises(self):
        with self.assertRaises(DialectError):
            str(User.query().for_update())

    def test_union_rejected(self):
        query = Pg.query().union(Pg.query()).for_update()
        with self.assertRaises(ValueError):
            str(query)


class TestTotalCursorExplain(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")

        class LiveUser(Model):
            tableName = "users"

        self.LiveUser = LiveUser
        LiveUser.bind(self.conn)
        LiveUser.query().insert([{"id": i, "name": f"u{i}"} for i in range(1, 8)]).run()

    def tearDown(self):
        self.LiveUser.unbind()
        self.conn.close()

    def test_total_ignores_order_and_limit(self):
        query = self.LiveUser.query().where("id", ">", 2).orderBy("id").limit(2)
        self.assertEqual(query.total(), 5)
        self.assertTrue(str(query).endswith("LIMIT 2"))

    def test_cursor_page_walks_the_table(self):
        first = self.LiveUser.query().cursor_page("id", 3).run()
        self.assertEqual([m.id for m in first], [1, 2, 3])
        second = self.LiveUser.query().cursor_page("id", 3, after=first[-1].id).run()
        self.assertEqual([m.id for m in second], [4, 5, 6])

    def test_explain_returns_plan_rows(self):
        plan = self.LiveUser.query().where("id", "=", 1).explain()
        self.assertGreater(len(plan), 0)

    def test_explain_raises_on_mssql(self):
        Ms = create_model("AnExplainMs", "t")
        Ms.set_dialect(Dialects.MSSQL)
        with self.assertRaises(DialectError):
            Ms.query().explain(self.conn)


if __name__ == "__main__":
    unittest.main()
