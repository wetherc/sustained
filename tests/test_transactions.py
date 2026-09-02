"""
Tests for transaction contexts, the statement listener, and the
executemany bulk insert path, against in-memory SQLite.
"""

import sqlite3
import unittest

from sustained import Model
from sustained.dialects import Dialects
from sustained.exceptions import DialectError
from sustained.execution import (
    cursor_scope,
    in_transaction,
    needs_explicit_begin,
    open_cursor,
    pinned_transaction,
    set_statement_listener,
    transaction,
)


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

    def test_rollback_takes_back_schema_changes(self):
        """
        The sqlite3 driver opens its implicit transaction before data
        statements only. A CREATE TABLE inside the block must still roll
        back, which needs the explicit BEGIN the context sends.
        """
        with self.assertRaises(RuntimeError):
            with transaction(self.conn):
                self.conn.execute("CREATE TABLE tx_ddl (id INTEGER)")
                raise RuntimeError("boom")
        remaining = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'tx_ddl'"
        ).fetchall()
        self.assertEqual([], remaining)

    def test_commit_keeps_schema_changes(self):
        with transaction(self.conn):
            self.conn.execute("CREATE TABLE tx_ddl (id INTEGER)")
        remaining = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'tx_ddl'"
        ).fetchall()
        self.assertEqual(1, len(remaining))

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


class RefusingCursor:
    """A cursor that refuses the statements its connection was told to fail."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        self._connection.statements.append(sql)
        if self._connection.refuse is not None and sql.startswith(
            self._connection.refuse
        ):
            raise sqlite3.OperationalError(f"refused: {sql}")
        return None

    def close(self):
        self._connection.closed_cursors += 1


class RefusingConnection:
    """
    A connection that records every statement and can be told to refuse the
    ones starting with a given prefix.
    """

    def __init__(self):
        self.statements = []
        self.refuse = None
        self.closed_cursors = 0

    def cursor(self):
        return RefusingCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class TestSavepointDepth(TransactionTestCase):
    def test_failed_savepoint_leaves_the_depth_where_it_was(self):
        """
        A SAVEPOINT that raises must still give the depth back, so the next
        nested block reuses the same savepoint name instead of skipping one.
        """
        conn = RefusingConnection()
        with transaction(conn):
            conn.refuse = "SAVEPOINT"
            with self.assertRaises(sqlite3.OperationalError):
                with transaction(conn):
                    pass
            conn.refuse = None
            with transaction(conn):
                pass
        self.assertEqual(
            conn.statements,
            [
                "SAVEPOINT sustained_sp_1",
                "SAVEPOINT sustained_sp_1",
                "RELEASE SAVEPOINT sustained_sp_1",
            ],
        )


class TestCursorScope(unittest.TestCase):
    """
    Every statement gives its cursor back, unless the cursor belongs to an
    open transaction() block.
    """

    def test_a_cursor_of_its_own_is_closed(self):
        conn = RefusingConnection()
        with cursor_scope(conn) as cursor:
            cursor.execute("SELECT 1")
        self.assertEqual(conn.closed_cursors, 1)

    def test_a_raising_block_still_closes_the_cursor(self):
        conn = RefusingConnection()
        with self.assertRaises(RuntimeError):
            with cursor_scope(conn):
                raise RuntimeError("boom")
        self.assertEqual(conn.closed_cursors, 1)

    def test_the_transactions_own_cursor_is_left_open(self):
        conn = RefusingConnection()
        with transaction(conn, Dialects.DUCKDB):
            with cursor_scope(conn) as cursor:
                self.assertIs(cursor, open_cursor(conn))
            self.assertEqual(conn.closed_cursors, 0)
        # The block closes its own cursor when it ends.
        self.assertEqual(conn.closed_cursors, 1)

    def test_a_pinned_block_closes_its_cursor_at_the_end(self):
        conn = RefusingConnection()
        with pinned_transaction(conn, Dialects.DUCKDB):
            self.assertEqual(conn.closed_cursors, 0)
        self.assertEqual(conn.closed_cursors, 1)


class TestPinnedTransaction(unittest.TestCase):
    """
    The block a rehearsal opens: registered like any transaction, ended by
    the caller.
    """

    def test_statements_inside_the_block_share_its_cursor(self):
        conn = RefusingConnection()
        with pinned_transaction(conn, Dialects.DUCKDB) as cursor:
            self.assertTrue(in_transaction(conn))
            self.assertIs(cursor, open_cursor(conn))
        self.assertFalse(in_transaction(conn))
        self.assertIsNot(cursor, open_cursor(conn))
        self.assertEqual(conn.statements, ["BEGIN"])

    def test_the_block_ends_nothing_by_itself(self):
        conn = RefusingConnection()
        with self.assertRaises(RuntimeError):
            with pinned_transaction(conn, Dialects.DUCKDB):
                raise RuntimeError("boom")
        self.assertEqual(conn.statements, ["BEGIN"])
        self.assertFalse(in_transaction(conn))

    def test_a_second_block_on_the_same_connection_is_refused(self):
        conn = RefusingConnection()
        with pinned_transaction(conn, Dialects.DUCKDB):
            with self.assertRaises(ValueError):
                with pinned_transaction(conn, Dialects.DUCKDB):
                    pass


class TestSavepointSpelling(unittest.TestCase):
    def test_mssql_savepoints_use_save_transaction(self):
        """
        T-SQL spells SAVEPOINT as SAVE TRANSACTION and has no RELEASE, so a
        successful inner block sends nothing on the way out.
        """
        conn = RefusingConnection()
        with transaction(conn, Dialects.MSSQL):
            with transaction(conn, Dialects.MSSQL):
                pass
            with self.assertRaises(RuntimeError):
                with transaction(conn, Dialects.MSSQL):
                    raise RuntimeError("boom")
        self.assertEqual(
            conn.statements,
            [
                "SAVE TRANSACTION sustained_sp_1",
                "SAVE TRANSACTION sustained_sp_1",
                "ROLLBACK TRANSACTION sustained_sp_1",
            ],
        )

    def test_nesting_refuses_where_the_dialect_has_no_savepoints(self):
        conn = RefusingConnection()
        with transaction(conn, Dialects.DUCKDB):
            with self.assertRaises(DialectError) as raised:
                with transaction(conn, Dialects.DUCKDB):
                    pass
        self.assertIn("DUCKDB", str(raised.exception))
        # The DuckDB driver runs autocommit, so the outer block opens and
        # commits in SQL; the refused nesting itself sent nothing.
        self.assertEqual(conn.statements, ["BEGIN", "COMMIT"])

    def test_duckdb_transactions_run_in_sql_on_one_cursor(self):
        conn = RefusingConnection()
        with self.assertRaises(RuntimeError):
            with transaction(conn, Dialects.DUCKDB):
                raise RuntimeError("boom")
        self.assertEqual(conn.statements, ["BEGIN", "ROLLBACK"])

    def test_a_refused_nesting_leaves_the_outer_block_usable(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        with transaction(conn, Dialects.DUCKDB):
            with self.assertRaises(DialectError):
                with transaction(conn, Dialects.DUCKDB):
                    pass
            conn.execute("INSERT INTO users (id) VALUES (1)")
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.assertEqual(total, 1)
        conn.close()

    def test_savepoint_statements_per_dialect(self):
        ansi = Dialects.get_compiler(Dialects.DEFAULT)
        self.assertEqual("SAVEPOINT sp", ansi.savepoint_sql("sp"))
        self.assertEqual("ROLLBACK TO SAVEPOINT sp", ansi.rollback_savepoint_sql("sp"))
        self.assertEqual("RELEASE SAVEPOINT sp", ansi.release_savepoint_sql("sp"))
        athena = Dialects.get_compiler(Dialects.ATHENA)
        self.assertIsNone(athena.savepoint_sql("sp"))
        self.assertIsNone(athena.rollback_savepoint_sql("sp"))
        self.assertIsNone(athena.release_savepoint_sql("sp"))
        mssql = Dialects.get_compiler(Dialects.MSSQL)
        self.assertIsNone(mssql.release_savepoint_sql("sp"))


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


class TestNeedsExplicitBegin(unittest.TestCase):
    """
    A driver with transaction control still needs a BEGIN when it leaves
    some statements outside the transaction it opens.
    """

    def test_a_legacy_sqlite3_connection_needs_one(self):
        conn = sqlite3.connect(":memory:")
        try:
            self.assertTrue(needs_explicit_begin(conn))
        finally:
            conn.close()

    def test_a_sqlite3_connection_in_a_transaction_does_not(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE t (a INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")
            self.assertTrue(conn.in_transaction)
            self.assertFalse(needs_explicit_begin(conn))
        finally:
            conn.close()

    def test_a_sqlite3_connection_in_the_new_control_does_not(self):
        conn = sqlite3.connect(":memory:", autocommit=False)
        try:
            self.assertFalse(needs_explicit_begin(conn))
        finally:
            conn.close()

    def test_another_driver_does_not(self):
        class OtherConnection:
            pass

        self.assertFalse(needs_explicit_begin(OtherConnection()))


if __name__ == "__main__":
    unittest.main()
