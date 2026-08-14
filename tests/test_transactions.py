"""
Tests for transaction contexts, the statement listener, and the
executemany bulk insert path, against in-memory SQLite.
"""

import sqlite3
import unittest

from sustained import Model
from sustained.execution import in_transaction, set_statement_listener


class TxUser(Model):
    tableName = "users"


class TransactionTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        TxUser.bind(self.conn)

    def tearDown(self):
        TxUser.unbind()
        self.conn.close()

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


class TestTransactions(TransactionTestCase):
    def test_commit_on_success(self):
        with TxUser.transaction():
            TxUser.query().insert({"id": 1, "name": "a"}).run()
            TxUser.query().insert({"id": 2, "name": "b"}).run()
        self.assertEqual(self.count(), 2)

    def test_rollback_on_exception(self):
        with self.assertRaises(RuntimeError):
            with TxUser.transaction():
                TxUser.query().insert({"id": 1, "name": "a"}).run()
                raise RuntimeError("boom")
        self.assertEqual(self.count(), 0)

    def test_nested_savepoint_rolls_back_inner_only(self):
        with TxUser.transaction():
            TxUser.query().insert({"id": 1, "name": "outer"}).run()
            with self.assertRaises(RuntimeError):
                with TxUser.transaction():
                    TxUser.query().insert({"id": 2, "name": "inner"}).run()
                    raise RuntimeError("inner boom")
            TxUser.query().insert({"id": 3, "name": "after"}).run()
        rows = self.conn.execute("SELECT id FROM users ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], [1, 3])

    def test_in_transaction_flag_clears(self):
        self.assertFalse(in_transaction(self.conn))
        with TxUser.transaction():
            self.assertTrue(in_transaction(self.conn))
        self.assertFalse(in_transaction(self.conn))

    def test_transaction_without_connection_raises(self):
        TxUser.unbind()
        with self.assertRaises(RuntimeError):
            with TxUser.transaction():
                pass

    def test_explicit_connection(self):
        other = sqlite3.connect(":memory:")
        other.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        with TxUser.transaction(other):
            TxUser.query().insert({"id": 9, "name": "x"}).run(other)
        total = other.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.assertEqual(total, 1)
        other.close()


class TestStatementListener(TransactionTestCase):
    def tearDown(self):
        set_statement_listener(None)
        super().tearDown()

    def test_listener_receives_sql_params_duration(self):
        seen = []
        set_statement_listener(lambda sql, params, dur: seen.append((sql, params, dur)))
        TxUser.query().insert({"id": 1, "name": "a"}).run()
        TxUser.query().where("id", "=", 1).run()
        self.assertEqual(len(seen), 2)
        self.assertIn("INSERT INTO users", seen[0][0])
        self.assertEqual(seen[1][1], (1,))
        self.assertGreaterEqual(seen[1][2], 0.0)

    def test_listener_removal(self):
        seen = []
        set_statement_listener(lambda *a: seen.append(a))
        set_statement_listener(None)
        TxUser.query().insert({"id": 1, "name": "a"}).run()
        self.assertEqual(seen, [])


class TestBulkInsertPath(TransactionTestCase):
    def test_multi_row_insert_uses_batch_and_counts(self):
        rows = [{"id": i, "name": f"u{i}"} for i in range(1, 51)]
        count = TxUser.query().insert(rows).run()
        self.assertEqual(count, 50)
        self.assertEqual(self.count(), 50)

    def test_batch_statement_is_single_row_template(self):
        seen = []
        set_statement_listener(lambda sql, params, dur: seen.append(sql))
        try:
            TxUser.query().insert(
                [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
            ).run()
        finally:
            set_statement_listener(None)
        self.assertEqual(seen[0].count("(?, ?)"), 1)

    def test_multi_row_with_returning_stays_single_statement(self):
        rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        result = TxUser.query().insert(rows).returning("id").run()
        self.assertEqual(result, [{"id": 1}, {"id": 2}])


if __name__ == "__main__":
    unittest.main()
