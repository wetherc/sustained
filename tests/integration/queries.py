"""
The read surface, run against every server whose support.json row claims
the `queries` cover. A server with a writable schema proves its reads with
a fixture round trip. Presto and Athena create nothing: each one reads a
catalog its server ships with, through a probe its subclass describes.
"""

import unittest

from sustained.dialects import Dialects
from sustained.migrations import Migrator
from sustained.model import Model

from . import harness, lifecycle


class QueriesCase(unittest.TestCase):
    """
    Base for one server's `queries` cover. Subclasses set NAME to a row in
    support.json and DIALECT to the dialect that row names. A server that
    only reads shipped catalogs sets READ_ONLY and describes its probe.
    """

    NAME = ""
    DIALECT = Dialects.DEFAULT
    # Presto and Athena read catalogs their servers ship with. Nothing is
    # created there, so the fixture tests skip and the probe runs instead.
    READ_ONLY = False
    # The probe for a read-only server: the table to read, then the column
    # to select, the column to filter on, and the value to filter with.
    # EXPECTED holds the exact values the probe returns, or None when the
    # catalog's contents are not fixed and any rows prove the read.
    SOURCE = ""
    PROBE = ("", "", None)
    EXPECTED = None

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
        if self.READ_ONLY:
            self.Reader = type("Reader", (Model,), {"_dialect": self.DIALECT})
            self.Reader.bind(self.connection)
            return
        self.Widget = lifecycle.make_model(
            "Widget", "it_widgets", self.DIALECT, lifecycle.widget_columns()
        )
        self.Widget.bind(self.connection)
        lifecycle.drop_everything(self.connection, self.DIALECT)

    def tearDown(self):
        if self.READ_ONLY:
            self.Reader.unbind()
            return
        lifecycle.drop_everything(self.connection, self.DIALECT)
        self.Widget.unbind()

    def test_a_query_round_trips_through_the_driver(self):
        if self.READ_ONLY:
            self.skipTest(f"{self.NAME} reads shipped catalogs only")
        Migrator(self.connection, [], dialect=self.DIALECT).up(models=[self.Widget])

        self.Widget.query().insert(
            [
                {"id": 1, "name": "hinge", "size": 3},
                {"id": 2, "name": "bracket", "size": 9},
            ]
        ).run()

        rows = self.Widget.query().where("size", ">", 5).run()
        self.assertEqual(["bracket"], [row.name for row in rows])

        self.Widget.query().update({"size": 12}).where("id", "=", 2).run()
        # first() caps the rows, and MSSQL will not cap without an order.
        updated = self.Widget.query().where("id", "=", 2).orderBy("id").first()
        self.assertEqual(12, updated.size)

        self.Widget.query().delete().where("id", "=", 1).run()
        self.assertEqual(1, len(self.Widget.query().run()))

    def test_a_parameterized_select_reads_the_shipped_catalog(self):
        if not self.READ_ONLY:
            self.skipTest(f"{self.NAME} proves its reads with the fixture round trip")
        column, where_column, where_value = self.PROBE
        query = (
            self.Reader.query()
            .select(column)
            .from_(self.SOURCE)
            .where(where_column, "=", where_value)
        )
        if self.EXPECTED is None:
            query = query.limit(5)
        rows = query.run()
        if self.EXPECTED is None:
            self.assertTrue(rows)
            self.assertTrue(all(getattr(row, column) for row in rows))
        else:
            self.assertEqual(
                list(self.EXPECTED), [getattr(row, column) for row in rows]
            )
