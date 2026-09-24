"""
Tests for INSERT, UPDATE, and DELETE statement building.
"""

import unittest

from sustained import DialectError, Model, RelationType, create_model
from sustained.dialects import Dialects

User = create_model("DmlUser", "users")


class DmlTeam(Model):
    tableName = "teams"


class DmlMember(Model):
    tableName = "users"
    relationMappings = {
        "team": {
            "relation": RelationType.BelongsToOneRelation,
            "modelClass": DmlTeam,
            "join": {"from": "users.team_id", "to": "teams.id"},
        }
    }


class TestInsert(unittest.TestCase):
    def test_single_row(self):
        sql, params = User.query().insert({"name": "a", "age": 3}).to_sql()
        self.assertEqual(sql, "INSERT INTO users (name, age) VALUES (?, ?)")
        self.assertEqual(params, ("a", 3))

    def test_multi_row(self):
        sql, params = User.query().insert([{"n": 1}, {"n": 2}]).to_sql()
        self.assertEqual(sql, "INSERT INTO users (n) VALUES (?), (?)")
        self.assertEqual(params, (1, 2))

    def test_inline_rendering(self):
        sql = str(User.query().insert({"name": "O'Brien"}))
        self.assertEqual(sql, "INSERT INTO users (name) VALUES ('O''Brien')")

    def test_mismatched_columns_raise(self):
        with self.assertRaises(ValueError):
            User.query().insert([{"a": 1}, {"b": 2}])

    def test_empty_rows_raise(self):
        with self.assertRaises(ValueError):
            User.query().insert([])
        with self.assertRaises(ValueError):
            User.query().insert({})

    def test_where_clause_rejected(self):
        query = User.query().insert({"a": 1}).where("id", "=", 1)
        with self.assertRaises(ValueError):
            str(query)

    def test_returning(self):
        sql, params = User.query().insert({"a": 1}).returning("id").to_sql()
        self.assertTrue(sql.endswith("RETURNING id"))

    def test_postgres_quoting(self):
        Pg = create_model("DmlPgUser", "users")
        Pg.set_dialect(Dialects.POSTGRES)
        sql, params = Pg.query().insert({"name": "x"}).to_sql()
        self.assertEqual(sql, 'INSERT INTO "users" ("name") VALUES (%s)')


class TestUpdate(unittest.TestCase):
    def test_update_with_where(self):
        sql, params = (
            User.query().update({"name": "b", "age": 4}).where("id", "=", 1).to_sql()
        )
        self.assertEqual(sql, "UPDATE users SET name = ?, age = ? WHERE id = ?")
        self.assertEqual(params, ("b", 4, 1))

    def test_update_without_where_raises(self):
        with self.assertRaises(ValueError):
            str(User.query().update({"a": 1}))

    def test_update_requires_values(self):
        with self.assertRaises(ValueError):
            User.query().update({})


class TestDelete(unittest.TestCase):
    def test_delete_with_where(self):
        sql, params = User.query().delete().where("id", "=", 1).to_sql()
        self.assertEqual(sql, "DELETE FROM users WHERE id = ?")
        self.assertEqual(params, (1,))

    def test_delete_without_where_raises(self):
        with self.assertRaises(ValueError):
            str(User.query().delete())


class TestUnrenderedWriteClauses(unittest.TestCase):
    """A write refuses every clause its SQL has no place for."""

    CLAUSES = {
        "select()": lambda q: q.select("id"),
        "distinct()": lambda q: q.distinct(),
        "distinctOn()": lambda q: q.distinctOn("id"),
        "from_()": lambda q: q.from_("other"),
        "with_()": lambda q: q.with_("recent", User.query().select("id")),
        "a join": lambda q: q.join("teams", "users.team_id", "=", "teams.id"),
        "groupBy()": lambda q: q.groupBy("team_id"),
        "having()": lambda q: q.having("team_id", ">", 1),
        "orderBy()": lambda q: q.orderBy("id"),
        "limit()": lambda q: q.limit(1),
        "offset()": lambda q: q.offset(1),
        "top()": lambda q: q.top(1),
        "union()": lambda q: q.union(User.query().select("id")),
        "qualify()": lambda q: q.qualify("n = 1"),
        "for_update()": lambda q: q.for_update(),
        "withGraphFetched()": lambda q: q.withGraphFetched("team"),
    }

    WRITES = {
        "INSERT": lambda: DmlMember.query().insert({"a": 1}),
        "UPDATE": lambda: DmlMember.query().update({"a": 1}).where("id", "=", 1),
        "DELETE": lambda: DmlMember.query().delete().where("id", "=", 1),
    }

    def test_each_clause_raises_for_each_write(self):
        for verb, write in self.WRITES.items():
            for name, add in self.CLAUSES.items():
                with self.subTest(verb=verb, clause=name):
                    query = add(write())
                    with self.assertRaises(ValueError) as caught:
                        query.to_sql()
                    self.assertIn(
                        f"{verb} statements do not render {name}.",
                        str(caught.exception),
                    )

    def test_insert_from_refuses_outer_limit(self):
        query = User.query().insert_from(None, User.query().select("id")).limit(1)
        with self.assertRaisesRegex(
            ValueError, "INSERT statements do not render limit"
        ):
            query.to_sql()

    def test_source_query_keeps_its_clauses(self):
        source = User.query().select("id").orderBy("id").limit(5)
        sql, _ = User.query().insert_from(["id"], source).to_sql()
        self.assertEqual(
            sql, "INSERT INTO users (id) SELECT id FROM users ORDER BY id ASC LIMIT 5"
        )

    def test_message_lists_every_dropped_clause(self):
        query = User.query().delete().where("id", ">", 1).orderBy("id").limit(1)
        with self.assertRaisesRegex(
            ValueError, r"DELETE statements do not render orderBy\(\), limit\(\)\. "
        ):
            str(query)

    def test_subquery_in_where_renders(self):
        oldest = User.query().select("id").orderBy("id").limit(1)
        sql, params = User.query().delete().whereIn("id", oldest).to_sql()
        self.assertEqual(
            sql,
            "DELETE FROM users WHERE id IN "
            "(SELECT id FROM users ORDER BY id ASC LIMIT 1)",
        )
        self.assertEqual(params, ())


class TestReturningDialects(unittest.TestCase):
    def test_mssql_returning_raises(self):
        Ms = create_model("DmlMsUser", "users")
        Ms.set_dialect(Dialects.MSSQL)
        with self.assertRaises(DialectError):
            str(Ms.query().insert({"a": 1}).returning("id"))

    def test_presto_returning_raises(self):
        Pr = create_model("DmlPrUser", "users")
        Pr.set_dialect(Dialects.PRESTO)
        with self.assertRaises(DialectError):
            str(Pr.query().insert({"a": 1}).returning("id"))


if __name__ == "__main__":
    unittest.main()
