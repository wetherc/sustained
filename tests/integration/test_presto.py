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
REGION = "tpch.tiny.region"


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

    def test_a_join_pairs_each_nation_with_its_region(self):
        rows = (
            self.Reader.query()
            .select(f"{NATION}.name AS nation_name")
            .from_(NATION)
            .innerJoin(REGION, f"{NATION}.regionkey", "=", f"{REGION}.regionkey")
            .where(f"{REGION}.name", "=", "AMERICA")
            .run()
        )
        self.assertEqual(
            {"ARGENTINA", "BRAZIL", "CANADA", "PERU", "UNITED STATES"},
            {row.nation_name for row in rows},
        )

    def test_a_window_function_ranks_nations_within_each_region(self):
        rows = (
            self.Reader.query()
            .select("name")
            .select_window(
                "RANK",
                "name_rank",
                partition_by=["regionkey"],
                order_by=["name"],
            )
            .from_(NATION)
            .run()
        )
        self.assertEqual(25, len(rows))
        first = {row.name for row in rows if int(row.name_rank) == 1}
        self.assertEqual({"ALGERIA", "ARGENTINA", "CHINA", "EGYPT", "FRANCE"}, first)

    def test_limit_caps_an_ordered_read(self):
        rows = (
            self.Reader.query()
            .select("name")
            .from_(NATION)
            .orderBy("name")
            .limit(3)
            .run()
        )
        self.assertEqual(["ALGERIA", "ARGENTINA", "BRAZIL"], [row.name for row in rows])

    def test_a_presto_spelled_function_runs_on_the_server(self):
        rows = (
            self.Reader.query()
            .select_func("approx_distinct", "regionkey", alias="regions")
            .from_(NATION)
            .run()
        )
        self.assertEqual(5, rows[0].regions)
