"""
Tests for the advisory lock the migrators hold while they run.
"""

import unittest

from sustained.dialects import Dialects
from sustained.migrations import Migration, Migrator


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def execute(self, sql, params=()):
        self._conn.log.append(sql)
        if self._conn.fail_on and self._conn.fail_on in sql:
            raise RuntimeError(f"forced failure on: {sql}")
        if sql.startswith("SELECT") and "checksum" in sql:
            self._rows = [
                (i, n, None, True, False) for n, i in enumerate(self._conn.applied, 1)
            ]
        elif sql.startswith("INSERT INTO") and "sustained_migrations" in sql:
            self._conn.applied.append(params[0])
        elif sql.startswith("DELETE FROM") and "sustained_migrations" in sql:
            self._conn.applied.remove(params[0])
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakePostgresConnection:
    def __init__(self, fail_on=None):
        self.applied = []
        self.log = []
        self.fail_on = fail_on

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.log.append("<commit>")

    def rollback(self):
        self.log.append("<rollback>")


class TestLockStatements(unittest.TestCase):
    def test_postgres_uses_advisory_locks(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        self.assertEqual(
            compiler.migration_lock_sql("sustained_migrations"),
            ["SELECT pg_advisory_lock(hashtext('sustained_migrations'))"],
        )
        self.assertEqual(
            compiler.migration_unlock_sql("sustained_migrations"),
            ["SELECT pg_advisory_unlock(hashtext('sustained_migrations'))"],
        )

    def test_mssql_uses_applocks(self):
        compiler = Dialects.get_compiler(Dialects.MSSQL)
        (lock,) = compiler.migration_lock_sql("sustained_migrations")
        self.assertIn("sp_getapplock", lock)
        self.assertIn("'sustained_migrations'", lock)
        self.assertIn("@LockOwner = 'Session'", lock)
        (unlock,) = compiler.migration_unlock_sql("sustained_migrations")
        self.assertIn("sp_releaseapplock", unlock)

    def test_engines_without_advisory_locks_return_nothing(self):
        for dialect in (
            Dialects.DEFAULT,
            Dialects.DUCKDB,
            Dialects.ATHENA,
            Dialects.PRESTO,
        ):
            compiler = Dialects.get_compiler(dialect)
            self.assertEqual(compiler.migration_lock_sql("t"), [])
            self.assertEqual(compiler.migration_unlock_sql("t"), [])


class TestBeginStatements(unittest.TestCase):
    """
    A rehearsal opens and closes its transaction with statements rather
    than driver calls, since SQLite starts a transaction for INSERT but not
    for CREATE TABLE, and asyncpg runs in autocommit until one is opened.
    """

    def test_engines_that_take_the_plain_spelling(self):
        for dialect in (
            Dialects.DEFAULT,
            Dialects.DUCKDB,
            Dialects.PRESTO,
            Dialects.POSTGRES,
        ):
            compiler = Dialects.get_compiler(dialect)
            self.assertEqual(compiler.begin_transaction_sql(), "BEGIN")
            self.assertEqual(compiler.rollback_transaction_sql(), "ROLLBACK")

    def test_mssql_spells_it_out(self):
        compiler = Dialects.get_compiler(Dialects.MSSQL)
        self.assertEqual(compiler.begin_transaction_sql(), "BEGIN TRANSACTION")
        self.assertEqual(compiler.rollback_transaction_sql(), "ROLLBACK TRANSACTION")

    def test_an_engine_without_transactions_has_neither(self):
        compiler = Dialects.get_compiler(Dialects.ATHENA)
        self.assertIsNone(compiler.begin_transaction_sql())
        self.assertIsNone(compiler.rollback_transaction_sql())


class TestLockingRun(unittest.TestCase):
    def _migrator(self, conn, migrations):
        return Migrator(conn, migrations, dialect=Dialects.POSTGRES)

    def test_up_takes_the_lock_before_reading_and_releases_after(self):
        conn = FakePostgresConnection()
        migration = Migration("one", up="CREATE TABLE t1 (id INTEGER)")
        self._migrator(conn, [migration]).up()
        lock_at = conn.log.index(
            "SELECT pg_advisory_lock(hashtext('sustained_migrations'))"
        )
        unlock_at = conn.log.index(
            "SELECT pg_advisory_unlock(hashtext('sustained_migrations'))"
        )
        ddl_at = conn.log.index("CREATE TABLE t1 (id INTEGER)")
        records_at = next(
            n
            for n, s in enumerate(conn.log)
            if s.startswith("SELECT") and "checksum" in s
        )
        self.assertLess(lock_at, records_at)
        self.assertLess(records_at, ddl_at)
        self.assertLess(ddl_at, unlock_at)

    def test_lock_is_released_when_a_step_fails(self):
        conn = FakePostgresConnection(fail_on="CREATE TABLE boom_t")
        migrator = self._migrator(
            conn, [Migration("boom", up="CREATE TABLE boom_t (id INTEGER)")]
        )
        with self.assertRaises(RuntimeError):
            migrator.up()
        self.assertIn(
            "SELECT pg_advisory_unlock(hashtext('sustained_migrations'))",
            conn.log,
        )

    def test_down_locks_too(self):
        conn = FakePostgresConnection()
        migration = Migration(
            "one", up="CREATE TABLE t1 (id INTEGER)", down="DROP TABLE t1"
        )
        migrator = self._migrator(conn, [migration])
        migrator.up()
        before = conn.log.count(
            "SELECT pg_advisory_lock(hashtext('sustained_migrations'))"
        )
        migrator.down()
        after = conn.log.count(
            "SELECT pg_advisory_lock(hashtext('sustained_migrations'))"
        )
        self.assertEqual(after, before + 1)

    def test_rehearsal_rolls_back_before_it_releases_the_lock(self):
        conn = FakePostgresConnection()
        migration = Migration(
            "one", up="CREATE TABLE t1 (id INTEGER)", down="DROP TABLE t1"
        )
        results = self._migrator(conn, [migration]).rehearse()
        self.assertEqual([(r.up_ok, r.down_ok) for r in results], [(True, True)])
        lock_at = conn.log.index(
            "SELECT pg_advisory_lock(hashtext('sustained_migrations'))"
        )
        begin_at = conn.log.index("BEGIN")
        ddl_at = conn.log.index("CREATE TABLE t1 (id INTEGER)")
        rollback_at = conn.log.index("ROLLBACK")
        unlock_at = conn.log.index(
            "SELECT pg_advisory_unlock(hashtext('sustained_migrations'))"
        )
        self.assertLess(lock_at, begin_at)
        self.assertLess(begin_at, ddl_at)
        self.assertLess(ddl_at, rollback_at)
        self.assertLess(rollback_at, unlock_at)
        # The reads before the rehearsal leave a transaction open on
        # psycopg, so it closes before BEGIN rather than warning.
        self.assertEqual(conn.log[begin_at - 1], "<rollback>")

    def test_rehearsal_commits_only_its_rehearsal_row(self):
        conn = FakePostgresConnection()
        migration = Migration(
            "one", up="CREATE TABLE t1 (id INTEGER)", down="DROP TABLE t1"
        )
        migrator = self._migrator(conn, [migration])
        migrator.applied_records()
        conn.log.clear()
        migrator.rehearse()
        rollback_at = conn.log.index("ROLLBACK")
        commits = [i for i, line in enumerate(conn.log) if line == "<commit>"]
        # Everything the rehearsal itself ran is inside the rolled-back
        # transaction; only the rehearsal row written afterwards commits.
        self.assertTrue(commits)
        self.assertTrue(all(i > rollback_at for i in commits))
        self.assertIn(
            'INSERT INTO "sustained_rehearsals" '
            "(rehearsal_key, outcome, rehearsed_at) VALUES (%s, %s, %s)",
            conn.log,
        )

    def test_the_row_lands_before_the_lock_is_released(self):
        conn = FakePostgresConnection()
        migration = Migration(
            "one", up="CREATE TABLE t1 (id INTEGER)", down="DROP TABLE t1"
        )
        self._migrator(conn, [migration]).rehearse()
        row_at = max(
            i
            for i, line in enumerate(conn.log)
            if line.startswith('INSERT INTO "sustained_rehearsals"')
        )
        unlock_at = conn.log.index(
            "SELECT pg_advisory_unlock(hashtext('sustained_migrations'))"
        )
        self.assertLess(row_at, unlock_at)


if __name__ == "__main__":
    unittest.main()
