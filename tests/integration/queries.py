"""
The read surface, run against every server whose support.json row claims
the `queries` cover. A server with a writable schema proves its reads with
a two-table fixture: joins, eager loading, aggregates, window functions,
CTEs, set operations, subqueries, paging, and every hydration mode.
Presto and Athena create nothing: each one reads a catalog its server
ships with, through a probe its subclass describes.
"""

import importlib.util
import unittest

from sustained.dialects import Dialects
from sustained.exceptions import DialectError
from sustained.execution import set_statement_listener
from sustained.migrations import Migrator
from sustained.model import Model
from sustained.schema import Integer, String
from sustained.types import RelationType

from . import harness, lifecycle


def maker_columns():
    return {
        "id": Integer(primary_key=True),
        "name": String(80, nullable=False),
    }


def widget_columns():
    columns = lifecycle.widget_columns()
    columns["maker_id"] = Integer()
    return columns


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
        self.Maker = lifecycle.make_model(
            "Maker", "it_makers", self.DIALECT, maker_columns()
        )
        self.Widget = lifecycle.make_model(
            "Widget", "it_widgets", self.DIALECT, widget_columns()
        )
        self.Widget.relationMappings = {
            "maker": {
                "relation": RelationType.BelongsToOneRelation,
                "modelClass": self.Maker,
                "join": {"from": "it_widgets.maker_id", "to": "it_makers.id"},
            }
        }
        self.Maker.relationMappings = {
            "widgets": {
                "relation": RelationType.HasManyRelation,
                "modelClass": self.Widget,
                "join": {"from": "it_makers.id", "to": "it_widgets.maker_id"},
            }
        }
        self.Maker.bind(self.connection)
        self.Widget.bind(self.connection)
        lifecycle.drop_everything(self.connection, self.DIALECT)

    def tearDown(self):
        if self.READ_ONLY:
            self.Reader.unbind()
            return
        lifecycle.drop_everything(self.connection, self.DIALECT)
        self.Widget.unbind()
        self.Maker.unbind()

    def seed(self):
        """The two-table fixture every read test starts from."""
        if self.READ_ONLY:
            self.skipTest(f"{self.NAME} reads shipped catalogs only")
        Migrator(self.connection, [], dialect=self.DIALECT).up(
            models=[self.Maker, self.Widget]
        )
        self.Maker.query().insert(
            [{"id": 1, "name": "acme"}, {"id": 2, "name": "zenith"}]
        ).run()
        self.Widget.query().insert(
            [
                {"id": 1, "name": "hinge", "size": 3, "maker_id": 1},
                {"id": 2, "name": "bracket", "size": 9, "maker_id": 1},
                {"id": 3, "name": "gear", "size": 7, "maker_id": 2},
                {"id": 4, "name": "cam", "size": 2, "maker_id": 2},
                {"id": 5, "name": "strut", "size": 5, "maker_id": None},
            ]
        ).run()

    def run_counting_statements(self, query):
        """Runs a query and reports (rows, how many statements it sent)."""
        sent = []
        set_statement_listener(lambda sql, params, duration: sent.append(sql))
        try:
            rows = query.run()
        finally:
            set_statement_listener(None)
        return rows, len(sent)

    # The driver round trip

    def test_a_query_round_trips_through_the_driver(self):
        self.seed()
        rows = self.Widget.query().where("size", ">", 5).run()
        self.assertEqual({"bracket", "gear"}, {row.name for row in rows})

        self.Widget.query().update({"size": 12}).where("id", "=", 2).run()
        # first() caps the rows, and MSSQL will not cap without an order.
        updated = self.Widget.query().where("id", "=", 2).orderBy("id").first()
        self.assertEqual(12, updated.size)

        self.Widget.query().delete().where("id", "=", 1).run()
        self.assertEqual(4, len(self.Widget.query().run()))

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

    # Joins and relations

    def test_an_inner_join_keeps_only_the_matched_rows(self):
        self.seed()
        rows = (
            self.Widget.query()
            .select("it_widgets.name")
            .innerJoinRelated("maker")
            .run()
        )
        self.assertEqual(
            {"hinge", "bracket", "gear", "cam"}, {row.name for row in rows}
        )

    def test_a_left_outer_join_keeps_the_unmatched_row(self):
        self.seed()
        rows = (
            self.Widget.query()
            .select("it_widgets.name", "it_makers.name AS maker_name")
            .leftOuterJoinRelated("maker")
            .run()
        )
        by_widget = {row.name: row.maker_name for row in rows}
        self.assertEqual(5, len(by_widget))
        self.assertEqual("acme", by_widget["hinge"])
        self.assertIsNone(by_widget["strut"])

    def test_eager_loading_a_belongs_to_costs_one_extra_query(self):
        self.seed()
        rows, statements = self.run_counting_statements(
            self.Widget.query().withGraphFetched("maker")
        )
        self.assertEqual(2, statements)
        by_widget = {row.name: row.maker for row in rows}
        self.assertEqual("acme", by_widget["bracket"].name)
        self.assertEqual("zenith", by_widget["gear"].name)
        self.assertIsNone(by_widget["strut"])

    def test_eager_loading_a_has_many_costs_one_extra_query(self):
        self.seed()
        rows, statements = self.run_counting_statements(
            self.Maker.query().withGraphFetched("widgets")
        )
        self.assertEqual(2, statements)
        by_maker = {row.name: sorted(w.name for w in row.widgets) for row in rows}
        self.assertEqual(["bracket", "hinge"], by_maker["acme"])
        self.assertEqual(["cam", "gear"], by_maker["zenith"])

    # Aggregates and window functions

    def test_an_aggregate_with_having_filters_the_groups(self):
        self.seed()
        rows = (
            self.Widget.query()
            .select("maker_id")
            .count("*", alias="widgets")
            .whereNotNull("maker_id")
            .groupBy("maker_id")
            .having("COUNT(*)", "=", 2)
            .run()
        )
        self.assertEqual({1, 2}, {row.maker_id for row in rows})
        self.assertEqual({2}, {row.widgets for row in rows})

    def test_a_window_function_ranks_within_each_partition(self):
        self.seed()
        rows = (
            self.Widget.query()
            .select("name")
            .select_window(
                "ROW_NUMBER",
                "size_rank",
                partition_by=["maker_id"],
                order_by=["size DESC"],
            )
            .whereNotNull("maker_id")
            .run()
        )
        ranked = {row.name: int(row.size_rank) for row in rows}
        self.assertEqual(1, ranked["bracket"])
        self.assertEqual(2, ranked["hinge"])
        self.assertEqual(1, ranked["gear"])
        self.assertEqual(2, ranked["cam"])

    # CTEs, set operations, and subqueries

    def test_a_cte_feeds_the_outer_query(self):
        self.seed()
        big = self.Widget.query().select("id", "name", "size").where("size", ">", 4)
        rows = (
            self.Widget.query()
            .with_("it_big", big)
            .select("name")
            .from_("it_big")
            .orderBy("size", "desc")
            .run()
        )
        self.assertEqual(["bracket", "gear", "strut"], [row.name for row in rows])

    def test_a_recursive_cte_builds_its_own_rows(self):
        self.seed()
        Anon = type("Anon", (Model,), {"_dialect": self.DIALECT})
        raw = Anon.query().raw
        counter = (
            Anon.query()
            .select(raw("1 AS n"))
            .unionAll(
                Anon.query().select(raw("n + 1")).from_("it_nums").where("n", "<", 5)
            )
        )
        rows = (
            Anon.query()
            .with_("it_nums", counter, recursive=True)
            .select("n")
            .from_("it_nums")
            .orderBy("n")
            .run(connection=self.connection)
        )
        self.assertEqual([1, 2, 3, 4, 5], [int(row.n) for row in rows])

    def test_set_operations_combine_result_sets(self):
        self.seed()
        widget_ids = self.Widget.query().select("id")
        maker_ids = self.Maker.query().select("id")

        union = widget_ids.clone().union(maker_ids.clone()).run()
        self.assertEqual({1, 2, 3, 4, 5}, {row.id for row in union})

        intersect = widget_ids.clone().intersect(maker_ids.clone()).run()
        self.assertEqual({1, 2}, {row.id for row in intersect})

        difference = widget_ids.clone().except_(maker_ids.clone()).run()
        self.assertEqual({3, 4, 5}, {row.id for row in difference})

    def test_a_subquery_filters_the_where_clause(self):
        self.seed()
        acme = self.Maker.query().select("id").where("name", "=", "acme")
        rows = self.Widget.query().whereIn("maker_id", acme).run()
        self.assertEqual({"hinge", "bracket"}, {row.name for row in rows})

    # Paging

    def test_limit_and_offset_follow_the_dialect(self):
        self.seed()
        rows = self.Widget.query().orderBy("id").limit(2).offset(1).run()
        self.assertEqual([2, 3], [row.id for row in rows])

        compiler = Dialects.get_compiler(self.DIALECT)
        try:
            compiler.compile_limit_offset(1, None, has_order_by=False)
        except DialectError:
            # MSSQL spells LIMIT as OFFSET FETCH, which needs an ORDER BY.
            # The refusal lands at build time, before anything is sent.
            with self.assertRaises(DialectError):
                self.Widget.query().limit(1).to_sql()
        else:
            self.assertEqual(1, len(self.Widget.query().limit(1).run()))

    # Hydration

    def test_rows_hydrate_into_every_format(self):
        self.seed()
        query = self.Widget.query().select("id", "name").orderBy("id").limit(2)

        models = query.run()
        self.assertTrue(all(isinstance(row, self.Widget) for row in models))

        dicts = query.to_dicts()
        self.assertEqual(
            [{"id": 1, "name": "hinge"}, {"id": 2, "name": "bracket"}], dicts
        )

        if importlib.util.find_spec("pandas") is None:
            self.skipTest("the pandas package is missing. Install pandas")
        frame = query.to_df()
        self.assertEqual(["hinge", "bracket"], list(frame["name"]))

        if importlib.util.find_spec("pyarrow") is None:
            self.skipTest("the pyarrow package is missing. Install pyarrow")
        table = query.to_arrow()
        self.assertEqual(["hinge", "bracket"], table.column("name").to_pylist())
