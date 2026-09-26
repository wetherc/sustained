"""
Async migration runner tests using the DbApiAsyncAdapter over SQLite.

These cover what only AsyncMigrator does: the async schema read, adapters
and async steps, and agreement with the sync migrator. The behaviour both
runners share is tested once, against each, in test_migrator_behaviour.py.
"""

import re
import sqlite3
import unittest
from unittest import mock

from sustained.aio import AsyncAdapter, DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.dialects import Dialects
from sustained.migrations import (
    Migration,
    Migrator,
    SchemaRead,
    _is_read_savepoint,
    _ReplayCursor,
)

# sqlite3.connect(autocommit=...) and Connection.autocommit arrived in
# Python 3.12.
HAS_SQLITE_AUTOCOMMIT = hasattr(sqlite3.Connection, "autocommit")

# What a rehearsal leaves behind: the tracking table and the row it
# earned, both created by the rehearsal itself.
SUSTAINED_TABLES = {"sustained_migrations", "sustained_rehearsals"}


def without_times(script):
    """The script with its applied_at literals blanked, so two runs compare."""
    return re.sub(r"'\d{4}-\d\d-\d\dT[^']*'", "'<time>'", script)


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r[0] for r in rows}


class TestAsyncMigrator(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    async def test_async_callable_step(self):
        seen = []

        async def make_it(adapter):
            await adapter.execute("CREATE TABLE cb_t (id INTEGER)", ())
            seen.append(True)

        migrator = AsyncMigrator(self.adapter, [Migration("cb", up=make_it)])
        await migrator.up()
        self.assertTrue(seen)
        self.assertIn("cb_t", table_names(self.conn))


class TestAsyncIntrospection(unittest.IsolatedAsyncioTestCase):
    """The async schema read returns what the blocking one returns."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    async def test_both_paths_report_the_same_schema(self):
        from sustained.autogenerate import async_introspect_schema, introspect_schema

        self.conn.execute("CREATE TABLE ai_users (id INTEGER PRIMARY KEY, bio TEXT)")
        self.conn.execute("CREATE INDEX ai_users_bio ON ai_users (bio)")
        self.conn.commit()
        self.assertEqual(
            await async_introspect_schema(self.adapter),
            introspect_schema(self.conn),
        )

    async def test_the_async_driver_degrades_to_columns(self):
        from sustained.autogenerate import async_introspect_schema
        from sustained.dialects import Dialects

        class Adapter:
            async def fetch(self, sql, params):
                if "table_constraints" in sql:
                    raise RuntimeError("no constraint views here")
                return [], [("shows", "id", "integer", "NO", None)]

        schema = await async_introspect_schema(Adapter(), Dialects.MSSQL)
        self.assertEqual(list(schema), ["shows"])
        self.assertEqual(schema["shows"].primary_key, ())

    async def test_a_guarded_read_takes_a_savepoint(self):
        # Postgres refuses every later statement in a transaction once
        # one has failed, so each query of the read runs inside a
        # savepoint and a failure rolls back to it.
        from sustained.autogenerate import async_introspect_schema
        from sustained.dialects import Dialects

        class Adapter:
            def __init__(self, savepoints=True):
                self.savepoints = savepoints
                self.log = []

            async def execute(self, sql, params):
                self.log.append(sql)
                if sql.startswith("SAVEPOINT") and not self.savepoints:
                    raise RuntimeError("no transaction is active")
                return 0

            async def fetch(self, sql, params):
                self.log.append(sql)
                if "pg_catalog.pg_index" in sql:
                    raise RuntimeError("no pg_index here")
                return [], []

        adapter = Adapter()
        await async_introspect_schema(adapter, Dialects.POSTGRES)
        self.assertIn("ROLLBACK TO SAVEPOINT sustained_read", adapter.log)
        self.assertIn("RELEASE SAVEPOINT sustained_read", adapter.log)

        bare = Adapter(savepoints=False)
        await async_introspect_schema(bare, Dialects.POSTGRES)
        self.assertEqual([s for s in bare.log if s.startswith("RELEASE")], [])
        self.assertTrue(any("information_schema.columns" in s for s in bare.log))

    async def test_a_guarded_read_keeps_the_first_error(self):
        from sustained.autogenerate import async_introspect_schema
        from sustained.dialects import Dialects

        class Adapter:
            def __init__(self):
                self.log = []

            async def execute(self, sql, params):
                self.log.append(sql)
                if sql.startswith("ROLLBACK"):
                    raise RuntimeError("no savepoint to roll back to")
                return 0

            async def fetch(self, sql, params):
                self.log.append(sql)
                if "pg_catalog.pg_index" in sql:
                    raise RuntimeError("no pg_index here")
                return [], []

        adapter = Adapter()
        await async_introspect_schema(adapter, Dialects.POSTGRES)
        self.assertIn("ROLLBACK TO SAVEPOINT sustained_read", adapter.log)

    async def test_a_guarded_read_releases_every_savepoint(self):
        # ROLLBACK TO SAVEPOINT leaves the savepoint in place, so a read
        # that only rolls back piles one savepoint up per failed query.
        from sustained.autogenerate import async_introspect_schema
        from sustained.dialects import Dialects

        class Adapter:
            def __init__(self):
                self.stack = []
                self.failures = 0

            async def execute(self, sql, params):
                word = sql.split()[0].upper()
                if word == "SAVEPOINT":
                    self.stack.append(sql.split()[1])
                elif word == "RELEASE" and self.stack:
                    self.stack.pop()
                return 0

            async def fetch(self, sql, params):
                if "pg_catalog" in sql:
                    self.failures += 1
                    raise RuntimeError("no such catalog")
                return [], []

        adapter = Adapter()
        await async_introspect_schema(adapter, Dialects.POSTGRES)
        self.assertGreater(adapter.failures, 1)
        self.assertEqual(adapter.stack, [])

    async def test_the_migrator_read_is_guarded_and_records_only_the_plan(self):
        # AsyncMigrator._read_schema() reads through the shared guarded
        # loop, so on Postgres one failed catalog probe does not poison
        # the transaction for every later statement. The savepoints the
        # guard takes stay out of the recording: a replay takes its own.
        from sustained.aio_migrations import AsyncMigrator
        from sustained.dialects import Dialects

        class Adapter:
            def __init__(self):
                self.log = []

            async def execute(self, sql, params):
                self.log.append(sql)
                return 0

            async def fetch(self, sql, params):
                self.log.append(sql)
                if "pg_catalog.pg_index" in sql:
                    raise RuntimeError("no pg_index here")
                return [], []

        adapter = Adapter()
        migrator = AsyncMigrator(adapter, [], dialect=Dialects.POSTGRES)
        _, read = await migrator._read_schema()
        self.assertIn("ROLLBACK TO SAVEPOINT sustained_read", adapter.log)
        self.assertIn("RELEASE SAVEPOINT sustained_read", adapter.log)
        recorded = [step.sql for step in read._steps]
        self.assertTrue(recorded)
        self.assertFalse(
            [s for s in recorded if "SAVEPOINT" in s],
            "the guard's savepoints must not be recorded",
        )
        failed = [step for step in read._steps if step.error is not None]
        self.assertTrue(any("pg_index" in step.sql for step in failed))

    async def test_a_refused_release_keeps_the_rows(self):
        # A driver may take a savepoint and refuse to release it. The
        # rows are already read, so the read keeps them.
        from sustained.autogenerate import async_introspect_schema
        from sustained.dialects import Dialects

        class Adapter:
            async def execute(self, sql, params):
                if sql.startswith("RELEASE"):
                    raise RuntimeError("this driver has no RELEASE SAVEPOINT")
                return 0

            async def fetch(self, sql, params):
                if "information_schema.columns" in sql:
                    return [], [
                        (
                            "users",
                            "id",
                            "integer",
                            "int4",
                            None,
                            None,
                            None,
                            "NO",
                            None,
                        )
                    ]
                return [], []

        schema = await async_introspect_schema(Adapter(), Dialects.POSTGRES)
        self.assertEqual(list(schema), ["users"])

    async def test_a_refused_savepoint_is_not_asked_for_again(self):
        # A connection in autocommit refuses every savepoint. The read
        # gives up on savepoints for the rest of the plan instead of
        # asking again before every statement.
        from sustained.autogenerate import async_introspect_schema
        from sustained.dialects import Dialects

        class Adapter:
            def __init__(self):
                self.attempts = 0
                self.reads = 0

            async def execute(self, sql, params):
                if sql.startswith("SAVEPOINT"):
                    self.attempts += 1
                    raise RuntimeError("no transaction is active")
                return 0

            async def fetch(self, sql, params):
                self.reads += 1
                return [], []

        adapter = Adapter()
        await async_introspect_schema(adapter, Dialects.POSTGRES)
        self.assertEqual(adapter.attempts, 1)
        self.assertGreater(adapter.reads, 1)

    async def test_a_failing_read_raises(self):
        from sustained.autogenerate import async_introspect_schema
        from sustained.dialects import Dialects

        with self.assertRaises(sqlite3.OperationalError):
            await async_introspect_schema(self.adapter, Dialects.POSTGRES)


class TestAsyncGeneratedDown(unittest.IsolatedAsyncioTestCase):
    """
    A migration generated from the models by the sync migrator, reverted
    by the async one. The statements come off the tracking row.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    def models(self):
        from sustained.model import Model
        from sustained.schema import Integer

        return [
            type(
                "AsyncGenUser",
                (Model,),
                {
                    "tableName": "async_gen_users",
                    "tableColumns": {"id": Integer(primary_key=True)},
                },
            )
        ]

    async def test_async_down_reverts_a_generated_migration(self):
        from sustained.migrations import Migrator

        Migrator(self.conn, []).up(models=self.models())
        self.assertIn("async_gen_users", table_names(self.conn))

        migrator = AsyncMigrator(self.adapter, [])
        applied_id = (await migrator.applied())[0]
        self.assertEqual(await migrator.down(), [applied_id])
        self.assertNotIn("async_gen_users", table_names(self.conn))
        self.assertEqual(await migrator.applied(), [])

    async def test_async_down_script_reverts_a_generated_migration(self):
        from sustained.migrations import Migrator

        Migrator(self.conn, []).up(models=self.models())
        migrator = AsyncMigrator(self.adapter, [])
        applied_id = (await migrator.applied())[0]
        script = await migrator.script("down")
        self.assertEqual(script, Migrator(self.conn, []).script("down"))
        self.assertIn(f"-- down: {applied_id}\nDROP TABLE", script)

    async def test_async_down_script_stops_at_a_generated_row_without_steps(self):
        from sustained.migrations import Migrator

        Migrator(self.conn, []).up(models=self.models())
        self.conn.execute("UPDATE sustained_migrations SET steps = NULL")
        script = await AsyncMigrator(self.adapter, []).script("down")
        self.assertTrue(script.endswith("has no reversible step; stopping"))


class TestAsyncRehearse(unittest.IsolatedAsyncioTestCase):
    """The async mirror of Migrator.rehearse()."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    def migrations(self):
        return [
            Migration("001_a", up="CREATE TABLE ra (id INTEGER)", down="DROP TABLE ra"),
            Migration("002_b", up="CREATE TABLE rb (id INTEGER)", down="DROP TABLE rb"),
            Migration("rv", up="CREATE VIEW rv1 AS SELECT 1", repeatable=True),
        ]

    async def test_rehearse_refuses_inside_an_open_transaction(self):
        from sustained.aio import async_transaction

        migrator = AsyncMigrator(self.adapter, self.migrations())
        async with async_transaction(self.adapter):
            with self.assertRaisesRegex(ValueError, "async_transaction"):
                await migrator.rehearse()

    async def test_a_callable_step_that_runs_arun_does_not_commit(self):
        from sustained.aio import async_transaction, in_async_transaction
        from sustained.model import Model
        from sustained.schema import Integer

        seed_model = type(
            "RehearsalSeed",
            (Model,),
            {
                "tableName": "ra",
                "tableColumns": {"id": Integer(primary_key=True)},
            },
        )
        seen = []

        async def seed(adapter):
            seen.append(in_async_transaction(adapter))
            await seed_model.query().insert({"id": 1}).arun(adapter)
            # A nested block takes a savepoint rather than a commit.
            async with async_transaction(adapter):
                await seed_model.query().insert({"id": 2}).arun()

        migrator = AsyncMigrator(
            self.adapter,
            [
                self.migrations()[0],
                Migration("002_seed", up=seed, down="DELETE FROM ra", checksum="s1"),
            ],
        )
        results = await migrator.rehearse()
        self.assertTrue(results.ok)
        self.assertEqual(seen, [True])
        self.assertFalse(in_async_transaction(self.adapter))
        self.assertEqual(table_names(self.conn), SUSTAINED_TABLES)

    async def test_pinned_async_transaction_refuses_a_second_block(self):
        from sustained.aio import pinned_async_transaction

        async with pinned_async_transaction(self.adapter):
            with self.assertRaisesRegex(ValueError, "already open"):
                async with pinned_async_transaction(self.adapter):
                    pass

    async def test_rehearsal_rolls_back_without_help_from_the_adapter(self):
        """
        asyncpg runs in autocommit until a transaction is opened, and its
        adapter's commit() and rollback() do nothing. The rehearsal must
        still take its changes back.
        """

        class AutocommitAdapter(DbApiAsyncAdapter):
            async def commit(self):
                pass

            async def rollback(self):
                pass

        adapter = AutocommitAdapter(self.conn)
        migrator = AsyncMigrator(adapter, self.migrations())
        results = await migrator.rehearse()
        self.assertEqual([r.up_ok for r in results], [True, True, True])
        self.assertEqual(table_names(self.conn), SUSTAINED_TABLES)
        self.assertEqual(await migrator.applied_records(), [])

    async def test_the_adapter_is_reachable(self):
        migrator = AsyncMigrator(self.adapter, [])
        self.assertIs(migrator.adapter, self.adapter)


class TestAsyncRehearsalRows(unittest.IsolatedAsyncioTestCase):
    """The async gate reads the same rows the sync one writes."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE TABLE gate_old (id INTEGER)")
        self.adapter = DbApiAsyncAdapter(self.conn)
        self.drop = Migration(
            "001_drop",
            up="DROP TABLE gate_old",
            down="CREATE TABLE gate_old (id INTEGER)",
        )

    def tearDown(self):
        self.conn.close()

    async def test_a_key_recorded_by_the_sync_migrator_is_accepted(self):
        from sustained.migrations import Migrator, rehearsal_key

        Migrator(self.conn, [self.drop]).record_rehearsal(
            rehearsal_key([], [self.drop])
        )
        migrator = AsyncMigrator(self.adapter, [self.drop])
        self.assertEqual(await migrator.up(), ["001_drop"])


class TestAsyncScript(unittest.IsolatedAsyncioTestCase):
    """script() renders the same text the sync migrator renders."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    def migrations(self):
        return [
            Migration("a", up="CREATE TABLE ta (id INTEGER)", down="DROP TABLE ta"),
            Migration("b", up="CREATE TABLE tb (id INTEGER)", down="DROP TABLE tb"),
            Migration(
                "v",
                up="CREATE VIEW va AS SELECT id FROM ta",
                repeatable=True,
            ),
        ]

    async def test_script_up_matches_the_sync_migrator(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        expected = Migrator(self.conn, self.migrations()).script("up")
        self.assertEqual(
            without_times(await migrator.script("up")), without_times(expected)
        )
        self.assertIn("-- up: a", expected)
        self.assertIn("-- repeat: v", expected)

    async def test_script_down_after_a_run_matches_the_sync_migrator(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        expected = Migrator(self.conn, self.migrations()).script("down")
        self.assertEqual(await migrator.script("down"), expected)  # no timestamps
        self.assertIn("-- down: b", expected)
        self.assertNotIn("-- down: v", expected)


class TestAsyncPlanAndDrift(unittest.IsolatedAsyncioTestCase):
    """plan() and drift() report what the sync migrator reports."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    def models(self):
        from sustained.model import Model
        from sustained.schema import Integer, Text

        return [
            type(
                "AsyncPlanUser",
                (Model,),
                {
                    "tableName": "async_plan_users",
                    "tableColumns": {
                        "id": Integer(primary_key=True),
                        "name": Text(),
                    },
                },
            )
        ]

    async def test_plan_matches_the_sync_migrator(self):
        migrator = AsyncMigrator(self.adapter, [])
        generated = await migrator.plan(self.models(), migration_id="auto_test")
        expected = Migrator(self.conn, []).plan(self.models(), migration_id="auto_test")
        self.assertEqual(generated.id, "auto_test")
        self.assertEqual(generated.up, expected.up)
        self.assertEqual(generated.down, expected.down)

    async def test_plan_refuses_a_not_null_column_it_cannot_probe(self):
        # plan() runs the schema read and nothing else on the async path,
        # so it cannot ask whether the table holds a row. An unprobeable
        # table counts as rows, and the NOT NULL column is refused even
        # where the blocking path would take it.
        from sustained.model import Model
        from sustained.schema import Integer, Text

        self.conn.execute("CREATE TABLE async_plan_users (id INTEGER PRIMARY KEY)")
        models = [
            type(
                "AsyncPlanNotNull",
                (Model,),
                {
                    "tableName": "async_plan_users",
                    "tableColumns": {
                        "id": Integer(primary_key=True),
                        "name": Text(nullable=False),
                    },
                },
            )
        ]
        migrator = AsyncMigrator(self.adapter, [])
        with self.assertRaises(ValueError) as caught:
            await migrator.plan(models)
        self.assertIn("without a default or backfill", str(caught.exception))
        # The blocking migrator probes the empty table and takes it.
        self.assertIsNotNone(Migrator(self.conn, []).plan(models))

    async def test_drift_matches_the_sync_migrator(self):
        migrator = AsyncMigrator(self.adapter, [])
        self.assertEqual(
            await migrator.drift(self.models()),
            Migrator(self.conn, []).drift(self.models()),
        )
        self.conn.execute("CREATE TABLE async_plan_users (id INTEGER PRIMARY KEY)")
        self.assertEqual(
            await migrator.drift(self.models()),
            ["column 'async_plan_users.name' was not added"],
        )


class TestRecordedSchemaRead(unittest.TestCase):
    """The recording a SchemaRead replays for the sync diffing code."""

    def test_a_replay_answers_the_recorded_rows(self):
        read = SchemaRead()
        read.record("SELECT 1", [(1,)])
        cursor = read.connection().cursor()
        cursor.execute("SELECT 1")
        self.assertEqual(cursor.rowcount, 1)
        self.assertEqual(cursor.fetchone(), (1,))
        self.assertEqual(cursor.fetchall(), [(1,)])
        self.assertIsNone(cursor.description)
        cursor.close()

    def test_a_recorded_failure_raises_again(self):
        read = SchemaRead()
        read.record("SELECT bad", [], ValueError("no such table"))
        cursor = read.connection().cursor()
        with self.assertRaises(ValueError):
            cursor.execute("SELECT bad")
        self.assertIsNone(cursor.fetchone())

    def test_a_statement_the_recording_does_not_hold_raises(self):
        cursor = SchemaRead().connection().cursor()
        with self.assertRaises(ValueError):
            cursor.execute("SELECT 1")

    def test_a_different_statement_at_this_position_raises(self):
        read = SchemaRead()
        read.record("SELECT 1 WHERE schema IN (current_schema())", [(1,)])
        cursor = read.connection().cursor()
        with self.assertRaises(ValueError) as caught:
            cursor.execute("SELECT 1 WHERE schema IN (current_schema(), 'app')")
        message = str(caught.exception)
        self.assertIn("position 0", message)
        self.assertIn("current_schema()", message)
        self.assertEqual(cursor.fetchall(), [])

    def test_the_connection_runs_nothing_of_its_own(self):
        connection = SchemaRead().connection()
        self.assertIsNone(connection.commit())
        self.assertIsNone(connection.rollback())
        self.assertIsNone(connection.close())
        with self.assertRaises(ValueError):
            connection.cursor().executemany("SELECT 1", [(1,)])


class RecordingAdapter(AsyncAdapter):
    """An adapter that answers every read with no rows and keeps the SQL."""

    def __init__(self):
        self.statements = []

    async def fetch(self, sql, params):
        self.statements.append(sql)
        return [], []


class TestReplayedSchemaScope(unittest.IsolatedAsyncioTestCase):
    """plan() replays exactly the read it recorded."""

    def models(self):
        from sustained.model import Model
        from sustained.schema import Integer

        return [
            type(
                "ScopedWidget",
                (Model,),
                {
                    "tableName": "widgets",
                    "tableSchema": "app",
                    "tableColumns": {"id": Integer(primary_key=True)},
                },
            )
        ]

    async def test_the_replay_asks_the_recorded_statements(self):
        adapter = RecordingAdapter()
        migrator = AsyncMigrator(adapter, [], dialect=Dialects.POSTGRES)
        asked = []
        recorded_execute = _ReplayCursor.execute

        def spy(self, operation, parameters=()):
            asked.append(operation)
            return recorded_execute(self, operation, parameters)

        with mock.patch.object(_ReplayCursor, "execute", spy):
            await migrator.plan(self.models())
        self.assertTrue(adapter.statements)
        queries = [sql for sql in asked if not _is_read_savepoint(sql)]
        self.assertEqual(queries, adapter.statements)

    async def test_the_replay_takes_the_savepoints_a_guarded_read_takes(self):
        adapter = RecordingAdapter()
        migrator = AsyncMigrator(adapter, [], dialect=Dialects.POSTGRES)
        asked = []
        recorded_execute = _ReplayCursor.execute

        def spy(self, operation, parameters=()):
            asked.append(operation)
            return recorded_execute(self, operation, parameters)

        with mock.patch.object(_ReplayCursor, "execute", spy):
            await migrator.plan(self.models())
        savepoints = [sql for sql in asked if _is_read_savepoint(sql)]
        self.assertTrue(savepoints)
        self.assertNotIn(savepoints[0], adapter.statements)

    async def test_the_read_covers_the_schemas_the_models_name(self):
        adapter = RecordingAdapter()
        migrator = AsyncMigrator(adapter, [], dialect=Dialects.POSTGRES)
        await migrator.plan(self.models())
        scoped = [s for s in adapter.statements if "'app'" in s]
        self.assertTrue(scoped)

    async def test_drift_reads_the_schemas_the_models_name(self):
        adapter = RecordingAdapter()
        migrator = AsyncMigrator(adapter, [], dialect=Dialects.POSTGRES)
        await migrator.drift(self.models())
        scoped = [s for s in adapter.statements if "'app'" in s]
        self.assertTrue(scoped)


if __name__ == "__main__":
    unittest.main()
