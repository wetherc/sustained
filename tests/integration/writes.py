"""
The write surface, run against every server whose support.json row claims
the `writes` cover: upserts through onConflict, RETURNING, INSERT ...
SELECT, CREATE TABLE AS, and the refusal to run UPDATE or DELETE without
a WHERE clause.

Where a dialect refuses a statement at build time, the refusal is the
test: DialectError comes out of to_sql() and nothing reaches the server.
"""

import unittest

from sustained.dialects import Dialects
from sustained.exceptions import DialectError
from sustained.introspect import introspect_schema
from sustained.migrations import Migrator

from . import harness, lifecycle


class WritesCase(unittest.TestCase):
    """
    Base for one server's `writes` cover. Subclasses set NAME to a row in
    support.json and DIALECT to the dialect that row names. HAS_RETURNING
    and HAS_CTAS say which statements the dialect builds; where a flag is
    False, the test asserts the build-time refusal instead.
    """

    NAME = ""
    DIALECT = Dialects.DEFAULT
    HAS_RETURNING = True
    HAS_CTAS = True

    @classmethod
    def setUpClass(cls):
        if not cls.NAME:
            raise unittest.SkipTest("base class")
        cls.connection = harness.connect(cls.NAME)

    @classmethod
    def tearDownClass(cls):
        connection = getattr(cls, "connection", None)
        if connection is not None:
            connection.close()

    def setUp(self):
        self.Widget = lifecycle.make_model(
            "Widget", "it_widgets", self.DIALECT, lifecycle.widget_columns()
        )
        self.Copy = lifecycle.make_model(
            "Copy", "it_widgets_copy", self.DIALECT, lifecycle.widget_columns()
        )
        self.Widget.bind(self.connection)
        self.Copy.bind(self.connection)
        lifecycle.drop_everything(self.connection, self.DIALECT)
        Migrator(self.connection, [], dialect=self.DIALECT).up(
            models=[self.Widget, self.Copy]
        )

    def tearDown(self):
        lifecycle.drop_everything(self.connection, self.DIALECT)
        self.Copy.unbind()
        self.Widget.unbind()

    def widgets(self):
        """Every it_widgets row, ordered by id, as (id, name, size)."""
        rows = self.Widget.query().orderBy("id").run()
        return [(row.id, row.name, row.size) for row in rows]

    # Upserts

    def test_an_upsert_updates_the_conflicting_row(self):
        self.Widget.query().insert({"id": 1, "name": "hinge", "size": 3}).run()
        (
            self.Widget.query()
            .insert({"id": 1, "name": "hinge", "size": 9})
            .onConflict("id")
            .merge()
            .run()
        )
        self.assertEqual([(1, "hinge", 9)], self.widgets())

    def test_an_upsert_ignore_keeps_the_existing_row(self):
        self.Widget.query().insert({"id": 1, "name": "hinge", "size": 3}).run()
        (
            self.Widget.query()
            .insert(
                [
                    {"id": 1, "name": "other", "size": 5},
                    {"id": 2, "name": "bracket", "size": 7},
                ]
            )
            .onConflict("id")
            .ignore()
            .run()
        )
        self.assertEqual([(1, "hinge", 3), (2, "bracket", 7)], self.widgets())

    # RETURNING

    def test_returning_hands_back_the_written_rows(self):
        if not self.HAS_RETURNING:
            self.skipTest(f"{self.NAME} refuses RETURNING at build time")
        rows = (
            self.Widget.query()
            .insert(
                [
                    {"id": 1, "name": "hinge", "size": 3},
                    {"id": 2, "name": "bracket", "size": 9},
                ]
            )
            .returning("id", "name")
            .run()
        )
        self.assertEqual(
            [{"id": 1, "name": "hinge"}, {"id": 2, "name": "bracket"}], rows
        )

        deleted = (
            self.Widget.query().delete().where("id", "=", 2).returning("name").run()
        )
        self.assertEqual([{"name": "bracket"}], deleted)

    def test_returning_refuses_at_build_time(self):
        if self.HAS_RETURNING:
            self.skipTest(f"{self.NAME} supports RETURNING")
        query = (
            self.Widget.query()
            .insert({"id": 1, "name": "hinge", "size": 3})
            .returning("id")
        )
        with self.assertRaises(DialectError):
            query.to_sql()
        self.assertEqual([], self.widgets())

    # INSERT ... SELECT

    def test_insert_from_copies_the_selected_rows(self):
        self.Widget.query().insert(
            [
                {"id": 1, "name": "hinge", "size": 3},
                {"id": 2, "name": "bracket", "size": 9},
            ]
        ).run()
        source = self.Widget.query().select("id", "name", "size").where("size", ">", 5)
        self.Copy.query().insert_from(["id", "name", "size"], source).run()

        rows = self.Copy.query().orderBy("id").run()
        self.assertEqual([(2, "bracket", 9)], [(r.id, r.name, r.size) for r in rows])

    # CREATE TABLE AS

    def test_create_table_as_materializes_the_select(self):
        if not self.HAS_CTAS:
            self.skipTest(f"{self.NAME} refuses CREATE TABLE AS at build time")
        self.Widget.query().insert(
            [
                {"id": 1, "name": "hinge", "size": 3},
                {"id": 2, "name": "bracket", "size": 9},
            ]
        ).run()
        (
            self.Widget.query()
            .select("id", "name")
            .where("size", ">", 5)
            .create_table_as("it_ctas")
            .run()
        )

        Ctas = lifecycle.make_model("Ctas", "it_ctas", self.DIALECT, {})
        Ctas.bind(self.connection)
        try:
            rows = Ctas.query().select("id", "name").orderBy("id").run()
            self.assertEqual([(2, "bracket")], [(r.id, r.name) for r in rows])
        finally:
            Ctas.unbind()

    def test_create_table_as_refuses_at_build_time(self):
        if self.HAS_CTAS:
            self.skipTest(f"{self.NAME} supports CREATE TABLE AS")
        query = self.Widget.query().select("id").create_table_as("it_ctas")
        with self.assertRaises(DialectError):
            query.to_sql()
        self.assertNotIn("it_ctas", introspect_schema(self.connection, self.DIALECT))

    # Guarded statements

    def test_update_without_a_where_clause_never_reaches_the_server(self):
        self.Widget.query().insert({"id": 1, "name": "hinge", "size": 3}).run()
        query = self.Widget.query().update({"size": 0})
        with self.assertRaises(ValueError) as raised:
            query.run()
        self.assertIn("WHERE", str(raised.exception))
        self.assertEqual([(1, "hinge", 3)], self.widgets())

    def test_delete_without_a_where_clause_never_reaches_the_server(self):
        self.Widget.query().insert({"id": 1, "name": "hinge", "size": 3}).run()
        query = self.Widget.query().delete()
        with self.assertRaises(ValueError) as raised:
            query.run()
        self.assertIn("WHERE", str(raised.exception))
        self.assertEqual([(1, "hinge", 3)], self.widgets())
