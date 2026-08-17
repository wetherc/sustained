"""
Presto and Trino: queries only.

Neither server takes the migration surface, so the check is that a
compiled query reaches the server, the placeholders match the driver, and
the rows come back hydrated. The tpch catalog ships with the container, so
nothing has to be created first.
"""

import unittest

from sustained.dialects import Dialects
from sustained.model import Model

from . import harness

NATION = "tpch.tiny.nation"


class PrestoQueries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.connection = harness.connect("presto")

    @classmethod
    def tearDownClass(cls):
        connection = getattr(cls, "connection", None)
        if connection is not None:
            connection.close()

    def setUp(self):
        self.Nation = type("Nation", (Model,), {"_dialect": Dialects.PRESTO})
        self.Nation.bind(self.connection)

    def tearDown(self):
        self.Nation.unbind()

    def test_a_parameterized_select_returns_rows(self):
        rows = (
            self.Nation.query()
            .select("name")
            .from_(NATION)
            .where("nationkey", "=", 1)
            .run()
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("ARGENTINA", rows[0].name)

    def test_an_aggregate_with_a_group_by_returns_one_row_per_region(self):
        rows = (
            self.Nation.query()
            .select("regionkey")
            .count("*", alias="nations")
            .from_(NATION)
            .groupBy("regionkey")
            .run()
        )
        self.assertEqual(5, len(rows))
        self.assertEqual({5}, {row.nations for row in rows})
