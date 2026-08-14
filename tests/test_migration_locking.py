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
        if sql.startswith("SELECT id, seq, checksum, success FROM"):
            self._rows = [
                (i, n, None, True) for n, i in enumerate(self._conn.applied, 1)
            ]
        elif sql.startswith("INSERT INTO"):
            self._conn.applied.append(params[0])
        elif sql.startswith("DELETE FROM"):
            self._conn.applied.remove(params[0])
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


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
            n for n, s in enumerate(conn.log) if s.startswith("SELECT id, seq")
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


if __name__ == "__main__":
    unittest.main()
