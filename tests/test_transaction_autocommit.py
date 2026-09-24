"""
transaction() on a connection the caller put in autocommit drives the
block with BEGIN, COMMIT and ROLLBACK statements.
"""

import sqlite3
import unittest

from sustained.execution import transaction

HAS_SQLITE_AUTOCOMMIT = hasattr(sqlite3.Connection, "autocommit")


class RecordingCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=()):
        self.log.append(sql)

    def close(self):
        pass


class AutocommitConnection:
    """A psycopg-like connection with autocommit on."""

    autocommit = True

    def __init__(self):
        self.log = []

    def cursor(self):
        return RecordingCursor(self.log)

    def commit(self):
        self.log.append("commit()")

    def rollback(self):
        self.log.append("rollback()")


class TestTransactionAutocommit(unittest.TestCase):
    def test_a_failed_block_sends_a_rollback_statement(self):
        conn = AutocommitConnection()
        with self.assertRaises(RuntimeError):
            with transaction(conn):
                raise RuntimeError("boom")
        self.assertEqual(conn.log, ["BEGIN", "ROLLBACK"])

    def test_a_finished_block_sends_a_commit_statement(self):
        conn = AutocommitConnection()
        with transaction(conn):
            pass
        self.assertEqual(conn.log, ["BEGIN", "COMMIT"])

    @unittest.skipUnless(HAS_SQLITE_AUTOCOMMIT, "sqlite3 autocommit needs 3.12")
    def test_sqlite_in_autocommit_rolls_the_block_back(self):
        conn = sqlite3.connect(":memory:", autocommit=True)
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE t (x INTEGER)")
        with self.assertRaises(RuntimeError):
            with transaction(conn):
                conn.execute("INSERT INTO t VALUES (1)")
                raise RuntimeError("boom")
        self.assertEqual(conn.execute("SELECT x FROM t").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
