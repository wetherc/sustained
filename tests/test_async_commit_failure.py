"""
A commit that fails at the end of async_transaction() rolls the block back.
"""

import sqlite3
import unittest

from sustained.aio import AsyncAdapter, DbApiAsyncAdapter, async_transaction
from sustained.dialects import Dialects


def deferred_fk_connection():
    """A SQLite connection whose foreign key is checked only at COMMIT."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE children (id INTEGER PRIMARY KEY, parent_id INTEGER "
        "REFERENCES parents (id) DEFERRABLE INITIALLY DEFERRED)"
    )
    return conn


class FailingCommitStatement(AsyncAdapter):
    """An adapter in autocommit whose COMMIT statement raises."""

    def __init__(self):
        self.statements = []

    async def execute(self, sql, params):
        self.statements.append(sql)
        if sql == "COMMIT":
            raise RuntimeError("commit refused")
        return 0


class TestAsyncCommitFailure(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_driver_commit_rolls_the_block_back(self):
        conn = deferred_fk_connection()
        self.addCleanup(conn.close)
        adapter = DbApiAsyncAdapter(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            async with async_transaction(adapter):
                await adapter.execute(
                    "INSERT INTO children (id, parent_id) VALUES (1, 99)", ()
                )
        self.assertFalse(conn.in_transaction)
        # A later write and its commit must not carry the failed block's row.
        await adapter.execute("INSERT INTO parents (id) VALUES (1)", ())
        await adapter.commit()
        rows = conn.execute("SELECT id FROM children").fetchall()
        self.assertEqual(rows, [])

    async def test_a_failed_commit_statement_sends_a_rollback(self):
        adapter = FailingCommitStatement()
        with self.assertRaises(RuntimeError):
            async with async_transaction(adapter, Dialects.DUCKDB):
                pass
        self.assertEqual(adapter.statements, ["BEGIN", "COMMIT", "ROLLBACK"])


if __name__ == "__main__":
    unittest.main()
