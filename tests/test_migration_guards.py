"""
Guards and callbacks on the migrator, sync and async, against in-memory
SQLite.
"""

import io
import sqlite3
import unittest
from contextlib import redirect_stderr

from sustained import Model
from sustained.aio import DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.dialects import Dialects
from sustained.exceptions import GuardBlocked
from sustained.guards import (
    BLOCK,
    Verdict,
    max_statements,
    no_drops,
    no_lock_without_timeout,
)
from sustained.migrations import Callbacks, Migration, Migrator, run_statements
from sustained.schema import Integer, String, Text


class GuardUser(Model):
    tableName = "guard_users"
    tableColumns = {
        "id": Integer(primary_key=True),
        "email": String(120, nullable=False),
    }


class GuardNote(Model):
    tableName = "guard_notes"
    tableColumns = {"id": Integer(primary_key=True), "body": Text()}


def create_users():
    return Migration(
        "001_users",
        up="CREATE TABLE guard_users (id INTEGER PRIMARY KEY, email TEXT)",
        down="DROP TABLE guard_users",
    )


def drop_users():
    return Migration("002_drop", up="DROP TABLE guard_users")


def rewrite_users():
    return Migration(
        "002_rewrite",
        up="ALTER TABLE guard_users ADD COLUMN bio TEXT NOT NULL",
    )


class RunStatementsTest(unittest.TestCase):
    def test_collects_every_sql_step_in_order(self):
        run = [
            Migration("a", up=["SELECT 1", "SELECT 2"]),
            Migration("b", up="SELECT 3"),
        ]
        self.assertEqual(run_statements(run), ["SELECT 1", "SELECT 2", "SELECT 3"])

    def test_skips_callable_steps(self):
        run = [Migration("a", up=lambda conn: None), Migration("b", up="SELECT 1")]
        self.assertEqual(run_statements(run), ["SELECT 1"])


class RunStatementsScopeTest(unittest.TestCase):
    def test_statements_name_their_migration(self):
        run = [
            Migration("a", up=["SELECT 1", "SELECT 2"]),
            Migration("b", up="SELECT 3", transactional=False),
        ]
        statements = run_statements(run)
        self.assertEqual([s.migration_id for s in statements], ["a", "a", "b"])
        self.assertEqual([s.transactional for s in statements], [True, True, False])

    def test_a_local_timeout_does_not_excuse_a_later_migration(self):
        run = [
            Migration(
                "001",
                up=[
                    "SET LOCAL lock_timeout = '5s'",
                    "ALTER TABLE guard_users ADD COLUMN bio TEXT",
                ],
            ),
            Migration("003", up="ALTER TABLE guard_users ADD COLUMN slug TEXT"),
        ]
        guard = no_lock_without_timeout()
        verdicts = guard(run_statements(run), Dialects.POSTGRES)
        self.assertEqual(
            [v.statement for v in verdicts],
            ["ALTER TABLE guard_users ADD COLUMN slug TEXT"],
        )

    def test_a_session_timeout_excuses_a_later_migration(self):
        run = [
            Migration("001", up="SET lock_timeout = '5s'"),
            Migration("003", up="ALTER TABLE guard_users ADD COLUMN slug TEXT"),
        ]
        guard = no_lock_without_timeout()
        self.assertEqual(guard(run_statements(run), Dialects.POSTGRES), [])


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def migrator(self, migrations, **kwargs):
        return Migrator(self.conn, migrations, **kwargs)


class GuardsReadMigrationBoundariesTest(GuardTestCase):
    def test_a_guard_written_against_strings_still_runs(self):
        def no_seed_data():
            def guard(statements, dialect):
                return [
                    Verdict("no_seed_data", BLOCK, s)
                    for s in statements
                    if s.upper().startswith("INSERT")
                ]

            return guard

        migrator = self.migrator(
            [
                create_users(),
                Migration("002_seed", up="INSERT INTO guard_users (id) VALUES (1)"),
            ],
            guards=[no_seed_data()],
        )
        with self.assertRaises(GuardBlocked):
            migrator.up()

    def test_a_guard_reads_the_migration_a_statement_came_from(self):
        seen = []

        def recorder():
            def guard(statements, dialect):
                seen.extend((s.migration_id, s.transactional) for s in statements)
                return []

            return guard

        migrator = self.migrator(
            [
                create_users(),
                Migration(
                    "002_index",
                    up="CREATE INDEX guard_users_email ON guard_users (email)",
                    transactional=False,
                ),
            ],
            guards=[recorder()],
        )
        migrator.up()
        self.assertEqual(seen, [("001_users", True), ("002_index", False)])


class MigratorGuardTest(GuardTestCase):
    def test_block_stops_the_run_before_any_statement(self):
        migrator = self.migrator([create_users()], guards=[no_drops()])
        migrator.up()
        migrator = self.migrator([create_users(), drop_users()], guards=[no_drops()])
        with self.assertRaises(GuardBlocked) as caught:
            migrator.up(unrehearsed=True)
        self.assertEqual(caught.exception.verdicts[0].rule, "no_drops")
        # The table the blocked statement would have dropped is still here.
        self.conn.execute("SELECT * FROM guard_users")

    def test_a_run_that_passes_the_guards_applies(self):
        migrator = self.migrator([create_users()], guards=[no_drops()])
        self.assertEqual(migrator.up(), ["001_users"])

    def test_warning_prints_and_the_run_goes_on(self):
        from sustained.guards import no_table_rewrite

        migrator = self.migrator(
            [create_users(), rewrite_users()], guards=[no_table_rewrite()]
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            applied = migrator.up()
        self.assertEqual(applied, ["001_users", "002_rewrite"])
        self.assertIn("warn: no_table_rewrite", stderr.getvalue())

    def test_no_guards_no_verdicts(self):
        migrator = self.migrator([create_users(), drop_users()])
        self.assertEqual(migrator.up(unrehearsed=True), ["001_users", "002_drop"])

    def test_guards_read_the_generated_migration(self):
        migrator = self.migrator([], guards=[max_statements(1)])
        migrator.up(models=[GuardUser])
        GuardUser.tableColumns["bio"] = Text()
        GuardUser.tableColumns["note"] = Text()
        try:
            migrator = self.migrator([], guards=[max_statements(1)])
            with self.assertRaises(GuardBlocked) as caught:
                migrator.up(models=[GuardUser])
            self.assertEqual(caught.exception.verdicts[0].rule, "max_statements(1)")
        finally:
            del GuardUser.tableColumns["bio"]
            del GuardUser.tableColumns["note"]

    def test_a_warning_prints_once_across_both_gates(self):
        from sustained.guards import no_table_rewrite

        migrator = self.migrator(
            [create_users(), rewrite_users()], guards=[no_table_rewrite()]
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            migrator.up(models=[GuardNote])
        self.assertEqual(stderr.getvalue().count("no_table_rewrite"), 1)


class MigratorCallbackTest(GuardTestCase):
    def test_callbacks_fire_around_a_run(self):
        seen = []
        callbacks = Callbacks(
            before_migrate=lambda conn: seen.append("before"),
            after_migrate=lambda conn, applied: seen.append(tuple(applied)),
        )
        migrator = self.migrator([create_users()], callbacks=callbacks)
        migrator.up()
        self.assertEqual(seen, ["before", ("001_users",)])

    def test_after_migrate_is_skipped_on_an_empty_run(self):
        seen = []
        callbacks = Callbacks(after_migrate=lambda conn, applied: seen.append(applied))
        migrator = self.migrator([create_users()], callbacks=callbacks)
        migrator.up()
        migrator = self.migrator([create_users()], callbacks=callbacks)
        migrator.up()
        self.assertEqual(len(seen), 1)

    def test_on_error_learns_the_failed_id(self):
        seen = []
        callbacks = Callbacks(
            on_error=lambda conn, migration_id, error: seen.append(migration_id)
        )
        broken = Migration("001_broken", up="NOT SQL")
        migrator = self.migrator([broken], callbacks=callbacks)
        with self.assertRaises(sqlite3.OperationalError):
            migrator.up()
        self.assertEqual(seen, ["001_broken"])

    def test_on_error_fires_when_a_guard_blocks(self):
        seen = []
        callbacks = Callbacks(
            on_error=lambda conn, migration_id, error: seen.append(type(error))
        )
        migrator = self.migrator(
            [drop_users()], guards=[no_drops()], callbacks=callbacks
        )
        with self.assertRaises(GuardBlocked):
            migrator.up(unrehearsed=True)
        self.assertEqual(seen, [GuardBlocked])

    def test_a_raising_on_error_leaves_the_run_error_in_place(self):
        def raiser(conn, migration_id, error):
            raise RuntimeError("callback broke")

        migrator = self.migrator(
            [Migration("001_broken", up="NOT SQL")],
            callbacks=Callbacks(on_error=raiser),
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(sqlite3.OperationalError):
                migrator.up()
        self.assertIn("on_error raised", stderr.getvalue())

    def test_a_raising_before_migrate_stops_the_run(self):
        def raiser(conn):
            raise RuntimeError("no")

        migrator = self.migrator(
            [create_users()], callbacks=Callbacks(before_migrate=raiser)
        )
        with self.assertRaises(RuntimeError):
            migrator.up()
        self.assertEqual(self.migrator([create_users()]).applied(), [])


class AsyncGuardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    def migrator(self, migrations, **kwargs):
        return AsyncMigrator(self.adapter, migrations, **kwargs)

    async def test_block_stops_an_async_run(self):
        migrator = self.migrator([drop_users()], guards=[no_drops()])
        with self.assertRaises(GuardBlocked):
            await migrator.up(unrehearsed=True)

    async def test_async_callbacks_are_awaited(self):
        seen = []

        async def before(adapter):
            seen.append("before")

        async def after(adapter, applied):
            seen.append(tuple(applied))

        migrator = self.migrator(
            [create_users()],
            callbacks=Callbacks(before_migrate=before, after_migrate=after),
        )
        await migrator.up()
        self.assertEqual(seen, ["before", ("001_users",)])

    async def test_plain_callbacks_also_run(self):
        seen = []
        migrator = self.migrator(
            [create_users()],
            callbacks=Callbacks(before_migrate=lambda adapter: seen.append(1)),
        )
        await migrator.up()
        self.assertEqual(seen, [1])

    async def test_async_on_error_learns_the_failed_id(self):
        seen = []

        async def on_error(adapter, migration_id, error):
            seen.append(migration_id)

        migrator = self.migrator(
            [Migration("001_broken", up="NOT SQL")],
            callbacks=Callbacks(on_error=on_error),
        )
        with self.assertRaises(sqlite3.OperationalError):
            await migrator.up()
        self.assertEqual(seen, ["001_broken"])

    async def test_a_raising_async_on_error_leaves_the_run_error_in_place(self):
        async def on_error(adapter, migration_id, error):
            raise RuntimeError("callback broke")

        migrator = self.migrator(
            [Migration("001_broken", up="NOT SQL")],
            callbacks=Callbacks(on_error=on_error),
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(sqlite3.OperationalError):
                await migrator.up()
        self.assertIn("on_error raised", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
