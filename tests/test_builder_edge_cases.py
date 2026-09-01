"""
Tests for query builder cases that used to render invalid SQL or bind
values to the wrong placeholders.
"""

import sqlite3
import unittest

from sustained import Model, create_model
from sustained.dialects import Dialects
from sustained.execution import set_statement_listener
from sustained.types import Expression


class EdgeWidget(Model):
    tableName = "widgets"


EdgePg = create_model("EdgePg", "widgets")
EdgePg.set_dialect(Dialects.POSTGRES)


class TestDistinctPairing(unittest.TestCase):
    def test_distinct_after_distinct_on_raises(self):
        query = EdgePg.query().distinctOn("name")
        with self.assertRaises(ValueError):
            query.distinct()

    def test_distinct_on_after_distinct_raises(self):
        query = EdgePg.query().distinct()
        with self.assertRaises(ValueError):
            query.distinctOn("name")


class TestIsOperator(unittest.TestCase):
    def test_is_with_a_value_raises(self):
        with self.assertRaises(ValueError) as caught:
            EdgeWidget.query().where("id", "IS", 5)
        self.assertIn("IS takes None", str(caught.exception))

    def test_is_not_with_a_value_raises(self):
        with self.assertRaises(ValueError):
            EdgeWidget.query().where("id", "IS NOT", "x")

    def test_is_with_none_still_renders_is_null(self):
        sql, _ = EdgeWidget.query().where("id", "IS", None).to_sql()
        self.assertIn("id IS NULL", sql)

    def test_is_with_a_boolean_is_allowed(self):
        query = EdgeWidget.query().where("flag", "IS", True)
        self.assertTrue(query._where_builder.has_clauses())

    def test_is_with_an_expression_is_allowed(self):
        query = EdgeWidget.query().where(
            "flag", "IS NOT", Expression("DISTINCT FROM 1")
        )
        self.assertTrue(query._where_builder.has_clauses())


class TestRawValueMarkers(unittest.TestCase):
    def test_question_mark_in_a_string_literal_is_not_a_marker(self):
        query = EdgeWidget.query().whereRaw("name LIKE 'who?%' AND id = ?", [7])
        sql, params = query.to_sql()
        self.assertEqual(params, (7,))
        self.assertIn("'who?%'", sql)

    def test_marker_count_still_checked_outside_quotes(self):
        with self.assertRaises(ValueError):
            EdgeWidget.query().whereRaw("id = ? AND name = ?", [1])

    def test_doubled_quote_inside_a_literal_is_escaped(self):
        query = EdgeWidget.query().whereRaw("name = 'it''s ?' AND id = ?", [3])
        sql, params = query.to_sql()
        self.assertEqual(params, (3,))
        self.assertIn("'it''s ?'", sql)

    def test_question_mark_in_a_quoted_identifier_is_not_a_marker(self):
        query = EdgeWidget.query().whereRaw('"od?d" = ?', [1])
        _, params = query.to_sql()
        self.assertEqual(params, (1,))


class TestMultiRowInsert(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT, note TEXT)"
        )
        EdgeWidget.bind(self.conn)

    def tearDown(self):
        set_statement_listener(None)
        EdgeWidget.unbind()
        self.conn.close()

    def test_expression_value_in_a_batch_insert_runs(self):
        EdgeWidget.query().insert(
            [
                {"id": 1, "name": "a", "note": Expression("upper('x')")},
                {"id": 2, "name": "b", "note": Expression("upper('y')")},
            ]
        ).run()

        rows = EdgeWidget.query().orderBy("id").to_dicts()
        self.assertEqual([r["note"] for r in rows], ["X", "Y"])

    def test_batch_insert_reports_its_values_to_the_listener(self):
        seen = []
        set_statement_listener(lambda sql, params, duration: seen.append(params))
        EdgeWidget.query().insert(
            [
                {"id": 1, "name": "a", "note": None},
                {"id": 2, "name": "b", "note": None},
            ]
        ).run()

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], (1, "a", None, 2, "b", None))

    def test_rows_with_different_columns_are_refused(self):
        with self.assertRaises(ValueError):
            EdgeWidget.query().insert([{"id": 1}, {"id": 2, "name": "b"}])


class TestCrossJoin(unittest.TestCase):
    def test_cross_join_without_a_condition(self):
        sql, _ = EdgeWidget.query().crossJoin("makers").to_sql()
        self.assertIn("CROSS JOIN makers", sql)
        self.assertNotIn("ON", sql)

    def test_plain_join_without_a_condition_still_raises(self):
        with self.assertRaises(ValueError):
            EdgeWidget.query().join("makers")

    def test_cross_join_with_a_condition_still_renders_on(self):
        query = EdgeWidget.query().crossJoin("makers", "widgets.m", "=", "makers.id")
        sql, _ = query.to_sql()
        self.assertIn("CROSS JOIN", sql)
        self.assertIn("ON", sql)


class TestJoinSpelling(unittest.TestCase):
    def test_capitalized_join_prefix_works(self):
        query = EdgeWidget.query().LeftJoin("makers", "widgets.m", "=", "makers.id")
        sql, _ = query.to_sql()
        self.assertIn("LEFT JOIN", sql)

    def test_unknown_attribute_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            EdgeWidget.query().sidewaysJoin
        self.assertFalse(hasattr(EdgeWidget.query(), "sidewaysJoin"))


if __name__ == "__main__":
    unittest.main()
