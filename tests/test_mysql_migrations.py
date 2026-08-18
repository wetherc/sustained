"""
Tests for running migrations against MySQL, where the schema changes do
not roll back.
"""

import asyncio
import unittest

from sustained.aio import AsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.dialects import Dialects
from sustained.migrations import Migration, Migrator

LOCK = "SELECT GET_LOCK('sustained_migrations', 31536000)"
UNLOCK = "SELECT RELEASE_LOCK('sustained_migrations')"


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
                (i, n, None, ok, False)
                for n, (i, ok) in enumerate(self._conn.applied, 1)
            ]
        elif sql.startswith("INSERT INTO") and "sustained_migrations" in sql:
            self._conn.rows.append(params)
            self._conn.applied.append((params[0], params[5]))
        elif sql.startswith("DELETE FROM") and "sustained_migrations" in sql:
            self._conn.applied = [r for r in self._conn.applied if r[0] != params[0]]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeMysqlConnection:
    def __init__(self, fail_on=None):
        self.applied = []
        self.rows = []
        self.log = []
        self.fail_on = fail_on

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.log.append("<commit>")

    def rollback(self):
        self.log.append("<rollback>")


def migrator(conn, migrations):
    return Migrator(conn, migrations, dialect=Dialects.MYSQL)


class TestMysqlRun(unittest.TestCase):
    def test_the_run_takes_the_advisory_lock(self):
        conn = FakeMysqlConnection()
        migrator(conn, [Migration("one", up="CREATE TABLE t1 (id INT)")]).up()
        self.assertLess(
            conn.log.index(LOCK), conn.log.index("CREATE TABLE t1 (id INT)")
        )
        self.assertLess(
            conn.log.index("CREATE TABLE t1 (id INT)"), conn.log.index(UNLOCK)
        )

    def test_a_migration_is_not_wrapped_in_a_transaction(self):
        conn = FakeMysqlConnection()
        migrator(conn, [Migration("one", up="CREATE TABLE t1 (id INT)")]).up()
        self.assertNotIn("<rollback>", conn.log)
        self.assertIn("<commit>", conn.log)

    def test_a_failed_migration_records_a_failure_row(self):
        conn = FakeMysqlConnection(fail_on="CREATE TABLE two")
        with self.assertRaises(RuntimeError):
            migrator(
                conn,
                [
                    Migration("one", up="CREATE TABLE one (id INT)"),
                    Migration("two", up="CREATE TABLE two (id INT)"),
                ],
            ).up()
        # The success flag is the sixth column of the tracking row.
        self.assertEqual(
            [(r[0], r[5]) for r in conn.rows], [("one", True), ("two", False)]
        )

    def test_the_lock_is_released_when_a_step_fails(self):
        conn = FakeMysqlConnection(fail_on="CREATE TABLE boom")
        with self.assertRaises(RuntimeError):
            migrator(conn, [Migration("boom", up="CREATE TABLE boom (id INT)")]).up()
        self.assertIn(UNLOCK, conn.log)


class TestMysqlRehearsal(unittest.TestCase):
    def test_rehearse_refuses_and_names_the_way_out(self):
        conn = FakeMysqlConnection()
        with self.assertRaises(ValueError) as caught:
            migrator(conn, [Migration("one", up="CREATE TABLE t1 (id INT)")]).rehearse()
        message = str(caught.exception)
        self.assertIn("mysql is not on that list", message)
        self.assertIn("scratch=True", message)
        self.assertIn("get_rehearsal_connection()", message)

    def test_a_scratch_rehearsal_runs(self):
        conn = FakeMysqlConnection()
        migration = Migration(
            "one", up="CREATE TABLE t1 (id INT)", down="DROP TABLE t1"
        )
        results = migrator(conn, [migration]).rehearse(scratch=True)
        self.assertEqual([(r.up_ok, r.down_ok) for r in results], [(True, True)])
        self.assertIn("CREATE TABLE t1 (id INT)", conn.log)
        self.assertIn("DROP TABLE t1", conn.log)

    def test_a_scratch_rehearsal_records_no_row(self):
        conn = FakeMysqlConnection()
        migration = Migration(
            "one", up="CREATE TABLE t1 (id INT)", down="DROP TABLE t1"
        )
        result = migrator(conn, [migration]).rehearse(scratch=True)
        self.assertFalse(result.recorded)
        self.assertFalse(
            any("sustained_rehearsals" in line for line in conn.log if "INSERT" in line)
        )


class RecordingAdapter(AsyncAdapter):
    def __init__(self):
        self.log = []

    async def fetch(self, sql, params):
        self.log.append(sql)
        return [], []

    async def execute(self, sql, params):
        self.log.append(sql)
        return 0

    async def commit(self):
        self.log.append("<commit>")

    async def rollback(self):
        self.log.append("<rollback>")


class TestAsyncMysqlRehearsal(unittest.TestCase):
    def test_async_rehearse_refuses_the_same_way(self):
        async def run():
            migrator = AsyncMigrator(
                RecordingAdapter(),
                [Migration("one", up="CREATE TABLE t1 (id INT)")],
                dialect=Dialects.MYSQL,
            )
            with self.assertRaises(ValueError) as caught:
                await migrator.rehearse()
            self.assertIn("mysql is not on that list", str(caught.exception))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
