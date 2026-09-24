"""
A CTE defined inside a subquery goes into the one WITH clause at the top of
the SELECT. MSSQL refuses a WITH inside parentheses, so the subquery renders
without its own.
"""

import sqlite3
import unittest

from sustained import QueryBuilder, create_model
from sustained.dialects import Dialects
from sustained.expressions import Subquery, col

Event = create_model("HoistEvent", "events")


def query(dialect: Dialects = Dialects.DEFAULT) -> QueryBuilder:
    return QueryBuilder(Event, dialect=dialect)


def recent(dialect: Dialects = Dialects.DEFAULT) -> QueryBuilder:
    """A subquery that reads a CTE it defines itself."""
    cte = query(dialect).select("id").where("day", ">", 5)
    return query(dialect).with_("recent", cte).from_("recent").select("id")


RECENT = "WITH recent AS (SELECT id FROM events WHERE day > ?) "


class TestHoisting(unittest.TestCase):
    def test_where_in_subquery(self):
        sql, params = query().where("kind", "=", "a").whereIn("id", recent()).to_sql()
        self.assertEqual(
            sql,
            RECENT + "SELECT * FROM events WHERE kind = ? "
            "AND id IN (SELECT id FROM recent)",
        )
        self.assertEqual(params, (5, "a"))

    def test_exists_subquery(self):
        sql, _ = query().whereExists(recent()).to_sql()
        self.assertEqual(
            sql, RECENT + "SELECT * FROM events WHERE EXISTS (SELECT id FROM recent)"
        )

    def test_select_list_subquery(self):
        sql, _ = query().select("id", Subquery(recent().limit(1), "r")).to_sql()
        self.assertEqual(
            sql,
            RECENT + "SELECT id, (SELECT id FROM recent LIMIT 1) AS r FROM events",
        )

    def test_join_condition_subquery(self):
        sql, _ = (
            query()
            .join("tags", lambda j: j.on("tags.event_id", "=", recent().limit(1)))
            .to_sql()
        )
        self.assertEqual(
            sql,
            RECENT + "SELECT * FROM events JOIN tags "
            "ON tags.event_id = (SELECT id FROM recent LIMIT 1)",
        )

    def test_predicate_subquery(self):
        sql, _ = query().where(col("id").in_(recent())).to_sql()
        self.assertEqual(
            sql, RECENT + "SELECT * FROM events WHERE id IN (SELECT id FROM recent)"
        )

    def test_subquery_inside_a_cte_body(self):
        outer = query().select("id").whereIn("id", recent())
        sql, params = query().with_("picked", outer).from_("picked").to_sql()
        self.assertEqual(
            sql,
            "WITH recent AS (SELECT id FROM events WHERE day > ?), "
            "picked AS (SELECT id FROM events WHERE id IN (SELECT id FROM recent)) "
            "SELECT * FROM picked",
        )
        self.assertEqual(params, (5,))

    def test_the_same_cte_reached_twice_renders_once(self):
        shared = recent()
        sql, params = query().whereIn("id", shared).whereExists(shared).to_sql()
        self.assertEqual(sql.count("recent AS"), 1)
        self.assertEqual(params, (5,))

    def test_one_name_for_two_bodies_raises(self):
        other = query().with_("recent", query().select("id")).from_("recent")
        with self.assertRaisesRegex(ValueError, "Duplicate CTE alias 'recent'"):
            query().whereIn("id", recent()).whereExists(other).to_sql()

    def test_mssql_renders_one_top_level_with(self):
        sql, _ = query(Dialects.MSSQL).whereIn("id", recent(Dialects.MSSQL)).to_sql()
        self.assertTrue(sql.startswith("WITH [recent] AS ("), sql)
        self.assertEqual(sql.count("WITH"), 1)

    def test_str_hoists_as_well(self):
        self.assertEqual(
            str(query().whereIn("id", recent())),
            "WITH recent AS (SELECT id FROM events WHERE day > 5) "
            "SELECT * FROM events WHERE id IN (SELECT id FROM recent)",
        )


class TestWritesKeepTheirOwnWith(unittest.TestCase):
    def test_delete_subqueries_each_keep_their_with(self):
        sql, params = (
            query().whereIn("id", recent()).whereIn("id", recent()).delete().to_sql()
        )
        self.assertEqual(
            sql,
            "DELETE FROM events WHERE id IN ("
            + RECENT
            + "SELECT id FROM recent) AND id IN ("
            + RECENT
            + "SELECT id FROM recent)",
        )
        self.assertEqual(params, (5, 5))


class TestSqliteRunsTheHoistedStatement(unittest.TestCase):
    def test_rows_match(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE events (id INTEGER, day INTEGER, kind TEXT)")
        connection.executemany(
            "INSERT INTO events VALUES (?, ?, ?)",
            [(1, 3, "a"), (2, 7, "a"), (3, 9, "b")],
        )
        sql, params = (
            query()
            .select("id")
            .where("kind", "=", "a")
            .whereIn("id", recent())
            .to_sql()
        )
        self.assertEqual(connection.execute(sql, params).fetchall(), [(2,)])
        connection.close()


if __name__ == "__main__":
    unittest.main()
