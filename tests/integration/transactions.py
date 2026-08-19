"""
The transaction surface, run against every server whose support.json row
claims the `transactions` cover: commit and rollback proven from a second
connection, savepoint nesting where the dialect has savepoints, and the
ConnectionPool checking connections out, returning them, and pinning one
for a transaction block.

The in-process servers run on a temporary database file instead of the
:memory: default, because a second :memory: connection would open a
different database and could observe nothing.
"""

import os
import shutil
import tempfile
import unittest

from sustained.dialects import Dialects
from sustained.exceptions import DialectError
from sustained.migrations import Migrator
from sustained.pool import ConnectionPool

from . import harness, lifecycle


class TransactionsCase(unittest.TestCase):
    """
    Base for one server's `transactions` cover. Subclasses set NAME to a
    row in support.json and DIALECT to the dialect that row names. Whether
    savepoint tests run is the compiler's call, not a class flag.
    """

    NAME = ""
    DIALECT = Dialects.DEFAULT

    @classmethod
    def setUpClass(cls):
        if not cls.NAME:
            raise unittest.SkipTest("base class")
        cls._dir = None
        cls._path = None
        row = harness.ROWS[cls.NAME]
        if row["server"] == "in-process" and harness.dsn(cls.NAME) == ":memory:":
            cls._dir = tempfile.mkdtemp(prefix="sustained-tx-")
            cls._path = os.path.join(cls._dir, "transactions.db")
        cls.connection = cls.open_connection()

    @classmethod
    def tearDownClass(cls):
        connection = getattr(cls, "connection", None)
        if connection is not None:
            connection.close()
        if cls._dir is not None:
            shutil.rmtree(cls._dir, ignore_errors=True)

    @classmethod
    def open_connection(cls):
        """One more connection to the same database, however it is served."""
        if cls._path is not None:
            return harness.driver(cls.NAME).connect(cls._path)
        return harness.connect(cls.NAME)

    def setUp(self):
        self.Widget = lifecycle.make_model(
            "Widget", "it_widgets", self.DIALECT, lifecycle.widget_columns()
        )
        self.Widget.bind(self.connection)
        lifecycle.drop_everything(self.connection, self.DIALECT)
        Migrator(self.connection, [], dialect=self.DIALECT).up(models=[self.Widget])

    def tearDown(self):
        lifecycle.drop_everything(self.connection, self.DIALECT)
        self.Widget.unbind()

    def compiler(self):
        return Dialects.get_compiler(self.DIALECT)

    def has_savepoints(self):
        return self.compiler().savepoint_sql("sustained_sp_1") is not None

    def insert(self, id_, name, size):
        self.Widget.query().insert({"id": id_, "name": name, "size": size}).run()

    def seen_elsewhere(self):
        """Every committed it_widgets row, read over a fresh connection."""
        other = self.open_connection()
        try:
            table = self.compiler().quote_identifier("it_widgets")
            cursor = other.cursor()
            cursor.execute(f"SELECT id, name, size FROM {table} ORDER BY id")
            return [tuple(row) for row in cursor.fetchall()]
        finally:
            other.close()

    # Commit and rollback

    def test_a_committed_block_is_visible_to_a_second_connection(self):
        with self.Widget.transaction():
            self.insert(1, "hinge", 3)
            self.insert(2, "bracket", 9)
        self.assertEqual([(1, "hinge", 3), (2, "bracket", 9)], self.seen_elsewhere())

    def test_a_rolled_back_block_leaves_nothing_behind(self):
        with self.assertRaises(RuntimeError):
            with self.Widget.transaction():
                self.insert(1, "hinge", 3)
                self.insert(2, "bracket", 9)
                raise RuntimeError("boom")
        self.assertEqual([], self.seen_elsewhere())

    # Savepoints

    def test_an_inner_rollback_keeps_the_outer_work(self):
        if not self.has_savepoints():
            self.skipTest(f"{self.NAME} has no savepoints")
        with self.Widget.transaction():
            self.insert(1, "hinge", 3)
            with self.assertRaises(RuntimeError):
                with self.Widget.transaction():
                    self.insert(2, "bracket", 9)
                    raise RuntimeError("inner boom")
            self.insert(3, "flange", 5)
        self.assertEqual([(1, "hinge", 3), (3, "flange", 5)], self.seen_elsewhere())

    def test_a_failed_statement_leaves_the_outer_block_usable(self):
        if not self.has_savepoints():
            self.skipTest(f"{self.NAME} has no savepoints")
        with self.Widget.transaction():
            self.insert(1, "hinge", 3)
            with self.assertRaises(Exception):
                with self.Widget.transaction():
                    self.insert(1, "duplicate", 0)
            with self.Widget.transaction():
                self.insert(2, "bracket", 9)
            self.insert(3, "flange", 5)
        self.assertEqual(
            [(1, "hinge", 3), (2, "bracket", 9), (3, "flange", 5)],
            self.seen_elsewhere(),
        )

    def test_nesting_refuses_where_the_dialect_has_no_savepoints(self):
        if self.has_savepoints():
            self.skipTest(f"{self.NAME} has savepoints")
        with self.Widget.transaction():
            self.insert(1, "hinge", 3)
            with self.assertRaises(DialectError):
                with self.Widget.transaction():
                    pass
            self.insert(2, "bracket", 9)
        self.assertEqual([(1, "hinge", 3), (2, "bracket", 9)], self.seen_elsewhere())

    # The pool

    def test_the_pool_reuses_a_returned_connection(self):
        pool = ConnectionPool(type(self).open_connection, max_size=2)
        try:
            self.Widget.bind(pool)
            self.insert(1, "hinge", 3)
            rows = self.Widget.query().orderBy("id").run()
            self.assertEqual([(1, "hinge", 3)], [(r.id, r.name, r.size) for r in rows])
            self.assertEqual(1, pool.size)
        finally:
            pool.close()

    def test_a_pool_transaction_pins_one_connection(self):
        pool = ConnectionPool(type(self).open_connection, max_size=2)
        try:
            self.Widget.bind(pool)
            with self.Widget.transaction():
                self.insert(1, "hinge", 3)
                rows = self.Widget.query().orderBy("id").run()
                self.assertEqual([1], [r.id for r in rows])
            self.assertEqual([(1, "hinge", 3)], self.seen_elsewhere())

            with self.assertRaises(RuntimeError):
                with self.Widget.transaction():
                    self.insert(2, "bracket", 9)
                    raise RuntimeError("boom")
            self.assertEqual([(1, "hinge", 3)], self.seen_elsewhere())
            self.assertEqual(1, pool.size)
        finally:
            pool.close()
