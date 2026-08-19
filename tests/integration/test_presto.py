"""
Presto and Trino: queries only.

Neither server takes the migration surface, so the check is that a
compiled query reaches the server, the placeholders match the driver, and
the rows come back hydrated. The tpch catalog ships with the container, so
nothing has to be created first.
"""

from sustained.dialects import Dialects

from . import queries

NATION = "tpch.tiny.nation"


class PrestoQueries(queries.QueriesCase):
    NAME = "presto"
    DIALECT = Dialects.PRESTO
    READ_ONLY = True
    SOURCE = NATION
    PROBE = ("name", "nationkey", 1)
    EXPECTED = ("ARGENTINA",)

    def test_an_aggregate_with_a_group_by_returns_one_row_per_region(self):
        rows = (
            self.Reader.query()
            .select("regionkey")
            .count("*", alias="nations")
            .from_(NATION)
            .groupBy("regionkey")
            .run()
        )
        self.assertEqual(5, len(rows))
        self.assertEqual({5}, {row.nations for row in rows})
