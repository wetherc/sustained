"""
Async migration runner tests using the DbApiAsyncAdapter over SQLite.
"""

import json
import re
import sqlite3
import unittest
from unittest import mock

from sustained.aio import AsyncAdapter, DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.dialects import Dialects
from sustained.exceptions import MigrationError, RehearsalRequired
from sustained.migrations import (
    REHEARSAL_FAILED,
    REHEARSAL_PASSED,
    Migration,
    Migrator,
    SchemaRead,
    _ReplayCursor,
)

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

    def migrations(self):
        return [
            Migration("a", up="CREATE TABLE ta (id INTEGER)", down="DROP TABLE ta"),
            Migration("b", up="CREATE TABLE tb (id INTEGER)", down="DROP TABLE tb"),
        ]

    async def test_up_applies_and_records(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        applied = await migrator.up()
        self.assertEqual(applied, ["a", "b"])
        self.assertIn("ta", table_names(self.conn))
        self.assertEqual(await migrator.pending(), [])
        self.assertEqual(await migrator.status(), [("a", True), ("b", True)])

    async def test_non_transactional_migration_applies_and_records(self):
        migrator = AsyncMigrator(
            self.adapter,
            [
                Migration(
                    "nt",
                    up="CREATE TABLE nt (id INTEGER)",
                    down="DROP TABLE nt",
                    transactional=False,
                )
            ],
        )
        self.assertEqual(await migrator.up(), ["nt"])
        self.assertIn("nt", table_names(self.conn))
        self.assertEqual(await migrator.applied(), ["nt"])
        self.assertEqual(await migrator.down(), ["nt"])
        self.assertNotIn("nt", table_names(self.conn))

    async def test_non_transactional_failure_records_a_failure_row(self):
        migrator = AsyncMigrator(
            self.adapter,
            [
                Migration(
                    "nt",
                    up=["CREATE TABLE nt (id INTEGER)", "THIS IS NOT SQL"],
                    transactional=False,
                )
            ],
        )
        with self.assertRaises(sqlite3.OperationalError):
            await migrator.up()
        row = self.conn.execute(
            "SELECT id, success FROM sustained_migrations"
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("nt", 0))
        self.assertEqual(await migrator.applied(), [])

    async def test_up_is_idempotent_and_targeted(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        self.assertEqual(await migrator.up(target="a"), ["a"])
        self.assertEqual(await migrator.up(target="a"), [])
        self.assertEqual(await migrator.up(), ["b"])

    async def test_down_and_down_to(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        self.assertEqual(await migrator.down(), ["b"])
        self.assertNotIn("tb", table_names(self.conn))
        await migrator.up()
        self.assertEqual(await migrator.down_to("a"), ["b"])
        self.assertEqual(await migrator.applied(), ["a"])

    async def test_down_refuses_a_step_count_below_one(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        for steps in (-1, 0):
            with self.subTest(steps=steps):
                with self.assertRaises(ValueError) as caught:
                    await migrator.down(steps=steps)
                self.assertIn("steps must be 1 or more", str(caught.exception))
        self.assertEqual(await migrator.applied(), ["a", "b"])

    async def test_down_refuses_a_migration_edited_after_it_applied(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        edited = self.migrations()
        edited[1] = Migration(
            "b", up="CREATE TABLE tb (id INTEGER, extra INTEGER)", down="DROP TABLE tb"
        )
        later = AsyncMigrator(self.adapter, edited)
        with self.assertRaises(MigrationError) as caught:
            await later.down()
        self.assertIn("changed after it was applied", str(caught.exception))
        self.assertEqual(await later.applied(), ["a", "b"])
        self.assertEqual(await later.down(allow_changed=True), ["b"])

    async def test_down_to_carries_the_changed_flag(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        edited = self.migrations()
        edited[1] = Migration(
            "b", up="CREATE TABLE tb (id INTEGER, extra INTEGER)", down="DROP TABLE tb"
        )
        later = AsyncMigrator(self.adapter, edited)
        with self.assertRaises(MigrationError):
            await later.down_to("a")
        self.assertEqual(await later.down_to("a", allow_changed=True), ["b"])
        self.assertEqual(await later.down_to("a"), [])

    async def test_failed_step_rolls_back(self):
        migrations = [
            Migration(
                "ok", up="CREATE TABLE ok_t (id INTEGER)", down="DROP TABLE ok_t"
            ),
            Migration("boom", up="THIS IS NOT SQL"),
        ]
        migrator = AsyncMigrator(self.adapter, migrations)
        with self.assertRaises(sqlite3.OperationalError):
            await migrator.up()
        self.assertEqual(await migrator.applied(), ["ok"])

    async def test_async_callable_step(self):
        seen = []

        async def make_it(adapter):
            await adapter.execute("CREATE TABLE cb_t (id INTEGER)", ())
            seen.append(True)

        migrator = AsyncMigrator(self.adapter, [Migration("cb", up=make_it)])
        await migrator.up()
        self.assertTrue(seen)
        self.assertIn("cb_t", table_names(self.conn))

    async def test_unknown_target_raises(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        with self.assertRaises(ValueError):
            await migrator.up(target="nope")

    async def test_apply_records_checksum_and_seq(self):
        from sustained.migrations import migration_checksum

        migrations = self.migrations()
        migrator = AsyncMigrator(self.adapter, migrations)
        await migrator.up()
        rows = self.conn.execute(
            "SELECT id, seq, checksum, success FROM sustained_migrations "
            "ORDER BY seq"
        ).fetchall()
        self.assertEqual(
            rows,
            [
                ("a", 1, migration_checksum(migrations[0]), 1),
                ("b", 2, migration_checksum(migrations[1]), 1),
            ],
        )

    async def test_legacy_tracking_table_is_upgraded(self):
        self.conn.execute(
            "CREATE TABLE sustained_migrations "
            "(id VARCHAR(255) PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "INSERT INTO sustained_migrations VALUES ('a', '2024-01-01T00:00:00')"
        )
        self.conn.commit()
        migrator = AsyncMigrator(self.adapter, self.migrations())
        self.assertEqual(await migrator.applied(), ["a"])
        self.assertEqual(await migrator.up(), ["b"])
        rows = self.conn.execute(
            "SELECT id, seq, success FROM sustained_migrations ORDER BY seq"
        ).fetchall()
        self.assertEqual(rows, [("a", 1, 1), ("b", 2, 1)])

    async def test_validate_detects_edit_and_repair_accepts_it(self):
        from sustained.exceptions import MigrationError

        await AsyncMigrator(
            self.adapter, [Migration("a", up="CREATE TABLE va (x INTEGER)")]
        ).up()
        edited = AsyncMigrator(
            self.adapter, [Migration("a", up="CREATE TABLE va (x BIGINT)")]
        )
        with self.assertRaises(MigrationError):
            await edited.validate()
        actions = await edited.repair()
        self.assertEqual(actions, ["updated the stored checksum of 'a'"])
        self.assertEqual(await edited.validate(), [])

    async def test_repair_leaves_a_changed_repeatable_pending(self):
        await AsyncMigrator(
            self.adapter,
            [Migration("r", up="CREATE VIEW rv AS SELECT 1", repeatable=True)],
        ).up()
        changed = AsyncMigrator(
            self.adapter,
            [Migration("r", up="CREATE VIEW rv2 AS SELECT 2", repeatable=True)],
        )
        self.assertEqual(await changed.repair(), [])
        self.assertEqual([m.id for m in await changed.pending()], ["r"])
        self.assertEqual(await changed.up(), ["r"])

    async def test_rehearse_rejects_autocommit_connections(self):
        self.conn.autocommit = True
        migrator = AsyncMigrator(self.adapter, self.migrations())
        with self.assertRaisesRegex(ValueError, "autocommit"):
            await migrator.rehearse()

    async def test_up_validates_by_default(self):
        from sustained.exceptions import MigrationError

        await AsyncMigrator(
            self.adapter, [Migration("a", up="CREATE TABLE va (x INTEGER)")]
        ).up()
        edited = AsyncMigrator(
            self.adapter,
            [
                Migration("a", up="CREATE TABLE va (x BIGINT)"),
                Migration("b", up="CREATE TABLE vb (x INTEGER)"),
            ],
        )
        with self.assertRaises(MigrationError):
            await edited.up()
        self.assertEqual(await edited.up(validate=False), ["b"])

    async def test_baseline_records_without_running(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        recorded = await migrator.baseline("a")
        self.assertEqual(recorded, ["a"])
        self.assertNotIn("ta", table_names(self.conn))
        row = self.conn.execute(
            "SELECT id, seq, execution_ms, success FROM sustained_migrations"
        ).fetchone()
        self.assertEqual(row, ("a", 1, None, 1))
        self.assertEqual(await migrator.up(), ["b"])

    async def test_baseline_unknown_target_raises(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        with self.assertRaises(ValueError):
            await migrator.baseline("nope")


class TestAsyncRepeatableMigrations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    def migrations(self, view_sql="SELECT id FROM t"):
        return [
            Migration("001_t", up="CREATE TABLE t (id INTEGER)", down="DROP TABLE t"),
            Migration(
                "active_view",
                up=f"CREATE VIEW IF NOT EXISTS v AS {view_sql}",
                repeatable=True,
            ),
        ]

    async def test_runs_after_versioned_and_records_once(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        self.assertEqual(await migrator.up(), ["001_t", "active_view"])
        self.assertEqual(await migrator.up(), [])
        records = {r.id: r for r in await migrator.applied_records()}
        self.assertEqual(records["active_view"].seq, 2)

    async def test_changed_checksum_reruns_and_updates_in_place(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        self.conn.execute("DROP VIEW v")
        changed = AsyncMigrator(
            self.adapter, self.migrations("SELECT id, id AS b FROM t")
        )
        self.assertEqual(await changed.up(), ["active_view"])
        records = await changed.applied_records()
        self.assertEqual(len(records), 2)
        self.assertEqual(await changed.validate(), [])

    async def test_statuses_reports_changed(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        changed = AsyncMigrator(
            self.adapter, self.migrations("SELECT id, id AS b FROM t")
        )
        self.assertEqual(
            await changed.statuses(),
            [("001_t", "applied"), ("active_view", "changed")],
        )
        self.assertEqual([m.id for m in await changed.pending()], ["active_view"])

    async def test_down_skips_repeatables(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        self.assertEqual(await migrator.down_to("001_t"), [])
        self.assertEqual(await migrator.down(), ["001_t"])

    async def test_repeatable_target_rejected(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        with self.assertRaisesRegex(ValueError, "repeatable"):
            await migrator.up(target="active_view")
        with self.assertRaisesRegex(ValueError, "repeatable"):
            await migrator.baseline("active_view")

    async def test_baseline_records_repeatables_at_current_checksum(self):
        self.conn.execute("CREATE TABLE t (id INTEGER)")
        self.conn.execute("CREATE VIEW v AS SELECT id FROM t")
        self.conn.commit()
        migrator = AsyncMigrator(self.adapter, self.migrations())
        self.assertEqual(await migrator.baseline("001_t"), ["001_t", "active_view"])
        self.assertEqual(await migrator.up(), [])


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

    async def test_rehearse_proves_both_directions_and_changes_nothing(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        results = await migrator.rehearse()
        self.assertEqual(
            [(r.id, r.up_ok, r.down_ok) for r in results],
            [("001_a", True, True), ("002_b", True, True), ("rv", True, None)],
        )
        self.assertEqual(table_names(self.conn), SUSTAINED_TABLES)
        self.assertEqual(await migrator.applied_records(), [])

    async def test_a_clean_sweep_proves_the_schema_came_back(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        results = await migrator.rehearse()
        self.assertEqual([r.reversed for r in results], [[], [], None])

    async def test_a_down_step_that_leaves_an_object_behind_is_reported(self):
        migrator = AsyncMigrator(
            self.adapter,
            [
                Migration(
                    "001_a",
                    up=[
                        "CREATE TABLE ra (id INTEGER)",
                        "CREATE TABLE ra_leftover (id INTEGER)",
                    ],
                    down="DROP TABLE ra",
                )
            ],
        )
        results = await migrator.rehearse()
        self.assertEqual(results[0].reversed, ["table 'ra_leftover' left behind"])

    async def test_nothing_pending_rehearses_nothing(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        self.assertEqual(await migrator.rehearse(), [])

    async def test_failing_up_step_stops_the_rehearsal(self):
        migrator = AsyncMigrator(
            self.adapter,
            [
                self.migrations()[0],
                Migration("002_bad", up="CRATE TABLE oops", down="DROP TABLE oops"),
            ],
        )
        results = await migrator.rehearse()
        self.assertEqual(
            [(r.id, r.up_ok) for r in results], [("001_a", True), ("002_bad", False)]
        )
        self.assertIn("syntax error", results[1].error)
        self.assertEqual(table_names(self.conn), SUSTAINED_TABLES)

    async def test_missing_and_failing_down_steps_are_reported(self):
        migrator = AsyncMigrator(
            self.adapter,
            [
                self.migrations()[0],
                Migration("002_forward", up="CREATE TABLE rf (id INTEGER)"),
            ],
        )
        results = await migrator.rehearse()
        self.assertEqual([r.error for r in results][1], "no down step")
        self.assertEqual(
            results[0].error, "down not reached: '002_forward' has no down step"
        )

        broken = AsyncMigrator(
            self.adapter,
            [
                self.migrations()[0],
                Migration(
                    "002_bad_down",
                    up="CREATE TABLE rbd (id INTEGER)",
                    down="DROP TABLE r_missing",
                ),
            ],
        )
        results = await broken.rehearse()
        self.assertEqual(results[1].down_ok, False)
        self.assertEqual(
            results[0].error, "down not reached: '002_bad_down' down failed"
        )

    async def test_rehearse_refuses_a_dialect_that_cannot_roll_back(self):
        from sustained.dialects import Dialects

        migrator = AsyncMigrator(
            self.adapter, self.migrations(), dialect=Dialects.ATHENA
        )
        with self.assertRaisesRegex(ValueError, "athena is not on that list"):
            await migrator.rehearse()
        # The dialect drives the check; SQLite's compiler keeps the
        # statements runnable here.
        migrator._compiler = Dialects.get_compiler(Dialects.DEFAULT)
        self.assertEqual(len(await migrator.rehearse(scratch=True)), 3)
        # A scratch rehearsal writes no row.
        self.assertEqual(table_names(self.conn), {"sustained_migrations"})

    async def test_rehearse_refuses_inside_an_open_transaction(self):
        from sustained.aio import async_transaction

        migrator = AsyncMigrator(self.adapter, self.migrations())
        async with async_transaction(self.adapter):
            with self.assertRaisesRegex(ValueError, "async_transaction"):
                await migrator.rehearse()

    async def test_validation_problems_stop_the_rehearsal(self):
        from sustained.exceptions import MigrationError

        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        edited = [
            Migration("001_a", up="CREATE TABLE ra (id TEXT)", down="DROP TABLE ra")
        ] + self.migrations()[1:]
        with self.assertRaises(MigrationError):
            await AsyncMigrator(self.adapter, edited).rehearse()

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

    async def test_a_passing_rehearsal_records_its_key(self):
        migrator = AsyncMigrator(self.adapter, [self.drop])
        rehearsal = await migrator.rehearse()
        self.assertTrue(rehearsal.ok)
        self.assertTrue(rehearsal.recorded)
        self.assertTrue(await migrator.rehearsed(rehearsal.key))
        self.assertEqual(
            await migrator.rehearsal_outcome(rehearsal.key), REHEARSAL_PASSED
        )

    async def test_a_failing_rehearsal_records_the_failure(self):
        broken = Migration("001_drop", up=["DROP TABLE gate_old", "NOT SQL"])
        migrator = AsyncMigrator(self.adapter, [broken])
        rehearsal = await migrator.rehearse()
        self.assertFalse(rehearsal.ok)
        self.assertEqual(
            await migrator.rehearsal_outcome(rehearsal.key), REHEARSAL_FAILED
        )

    async def test_an_unrehearsed_drop_is_refused(self):
        migrator = AsyncMigrator(self.adapter, [self.drop])
        with self.assertRaises(RehearsalRequired) as caught:
            await migrator.up()
        self.assertIn("001_drop  DROP TABLE gate_old", str(caught.exception))
        self.assertEqual(await migrator.applied(), [])

    async def test_a_rehearsed_drop_runs(self):
        migrator = AsyncMigrator(self.adapter, [self.drop])
        await migrator.rehearse()
        self.assertEqual(await migrator.up(), ["001_drop"])
        self.assertNotIn("gate_old", table_names(self.conn))

    async def test_the_override_runs_without_a_rehearsal_row(self):
        migrator = AsyncMigrator(self.adapter, [self.drop])
        self.assertEqual(await migrator.up(unrehearsed=True), ["001_drop"])

    async def test_an_additive_run_is_never_gated(self):
        additive = Migration("001_add", up="CREATE TABLE gate_new (id INTEGER)")
        migrator = AsyncMigrator(self.adapter, [additive])
        self.assertEqual(await migrator.up(), ["001_add"])

    async def test_an_unknown_outcome_is_refused(self):
        migrator = AsyncMigrator(self.adapter, [])
        with self.assertRaises(ValueError):
            await migrator.record_rehearsal("0" * 64, "maybe")

    async def test_an_unknown_key_has_no_outcome(self):
        migrator = AsyncMigrator(self.adapter, [])
        self.assertIsNone(await migrator.rehearsal_outcome("0" * 64))
        self.assertFalse(await migrator.rehearsed("0" * 64))

    async def test_a_targeted_run_uses_the_prefix_the_rehearsal_proved(self):
        later = Migration(
            "002_add",
            up="CREATE TABLE gate_new (id INTEGER)",
            down="DROP TABLE gate_new",
        )
        migrator = AsyncMigrator(self.adapter, [self.drop, later])
        self.assertTrue((await migrator.rehearse()).ok)
        self.assertEqual(await migrator.up(target="001_drop"), ["001_drop"])
        self.assertNotIn("gate_old", table_names(self.conn))
        self.assertNotIn("gate_new", table_names(self.conn))

    async def test_a_key_recorded_by_the_sync_migrator_is_accepted(self):
        from sustained.migrations import Migrator, rehearsal_key

        Migrator(self.conn, [self.drop]).record_rehearsal(
            rehearsal_key([], [self.drop])
        )
        migrator = AsyncMigrator(self.adapter, [self.drop])
        self.assertEqual(await migrator.up(), ["001_drop"])


if __name__ == "__main__":
    unittest.main()


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

    async def test_script_writes_nothing(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.script("up")
        self.assertEqual(table_names(self.conn) & SUSTAINED_TABLES, set())
        self.assertEqual(await migrator.read_applied_records(), [])
        self.assertEqual(await migrator.read_applied(), [])
        self.assertEqual(
            [m.id for m in await migrator.pending()],
            [m.id for m in self.migrations()],
        )
        self.assertEqual(table_names(self.conn) & SUSTAINED_TABLES, set())

    async def test_script_down_after_a_run_matches_the_sync_migrator(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        expected = Migrator(self.conn, self.migrations()).script("down")
        self.assertEqual(await migrator.script("down"), expected)  # no timestamps
        self.assertIn("-- down: b", expected)
        self.assertNotIn("-- down: v", expected)

    async def test_script_reads_the_rows_a_run_left(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up(target="a")
        script = await migrator.script("up")
        self.assertNotIn("-- up: a", script)
        self.assertIn("-- up: b", script)
        self.assertEqual(await migrator.read_applied(), ["a"])

    async def test_script_rejects_an_unknown_direction(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        with self.assertRaises(ValueError):
            await migrator.script("sideways")


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

    async def test_plan_writes_nothing(self):
        migrator = AsyncMigrator(self.adapter, [])
        await migrator.plan(self.models())
        self.assertEqual(table_names(self.conn), set())
        await migrator.drift(self.models())
        self.assertEqual(table_names(self.conn), set())

    async def test_plan_generates_its_own_id(self):
        migrator = AsyncMigrator(self.adapter, [])
        generated = await migrator.plan(self.models())
        self.assertTrue(generated.id.startswith("auto_"))

    async def test_plan_returns_none_when_the_schema_matches(self):
        migrator = AsyncMigrator(self.adapter, [])
        self.conn.execute(
            "CREATE TABLE async_plan_users (id INTEGER PRIMARY KEY, name TEXT)"
        )
        self.assertIsNone(await migrator.plan(self.models()))
        self.assertEqual(await migrator.drift(self.models()), [])

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

    async def test_drift_ignores_changed_columns_on_request(self):
        migrator = AsyncMigrator(self.adapter, [])
        self.conn.execute(
            "CREATE TABLE async_plan_users (id INTEGER PRIMARY KEY, name INTEGER)"
        )
        self.assertTrue(await migrator.drift(self.models()))
        self.assertEqual(
            await migrator.drift(self.models(), ignore_changed_columns=True), []
        )

    async def test_the_tracking_table_is_left_out_of_the_diff(self):
        migrator = AsyncMigrator(self.adapter, [])
        await migrator.up()
        self.conn.execute(
            "CREATE TABLE async_plan_users (id INTEGER PRIMARY KEY, name TEXT)"
        )
        self.assertEqual(await migrator.drift(self.models()), [])
        self.assertIsNone(await migrator.plan(self.models(), allow_drops=True))


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


class TestAsyncModelRuns(unittest.IsolatedAsyncioTestCase):
    """up(models=...) and rehearse(models=...) on the async migrator."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    def models(self, columns=None):
        from sustained.model import Model
        from sustained.schema import Integer, Text

        return [
            type(
                "AsyncRunUser",
                (Model,),
                {
                    "tableName": "async_run_users",
                    "tableColumns": columns
                    or {"id": Integer(primary_key=True), "name": Text()},
                },
            )
        ]

    def migrations(self):
        return [Migration("a", up="CREATE TABLE ta (id INTEGER)", down="DROP TABLE ta")]

    def rows(self):
        return self.conn.execute(
            "SELECT id, generated, steps FROM sustained_migrations ORDER BY seq"
        ).fetchall()

    async def test_up_applies_the_generated_migration_last(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        applied = await migrator.up(models=self.models())
        self.assertEqual(applied[0], "a")
        self.assertTrue(applied[1].startswith("auto_"))
        self.assertIn("async_run_users", table_names(self.conn))
        self.assertEqual(await migrator.drift(self.models()), [])

    async def test_the_generated_statements_live_on_the_tracking_row(self):
        migrator = AsyncMigrator(self.adapter, [])
        await migrator.up(models=self.models(), migration_id="auto_run")
        rows = self.rows()
        self.assertEqual(rows[0][0], "auto_run")
        self.assertEqual(rows[0][1], 1)
        stored = json.loads(rows[0][2])
        self.assertEqual(
            stored["up"],
            ["CREATE TABLE async_run_users (id INTEGER PRIMARY KEY, name TEXT)"],
        )
        self.assertEqual(stored["down"], ["DROP TABLE IF EXISTS async_run_users"])

    async def test_a_generated_migration_reverts_from_its_row(self):
        migrator = AsyncMigrator(self.adapter, [])
        await migrator.up(models=self.models(), migration_id="auto_run")
        self.assertEqual(await migrator.down(), ["auto_run"])
        self.assertNotIn("async_run_users", table_names(self.conn))

    async def test_a_registered_migration_stores_no_statements(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up(models=self.models())
        self.assertEqual(self.rows()[0], ("a", 0, None))

    async def test_a_second_run_generates_nothing(self):
        migrator = AsyncMigrator(self.adapter, [])
        await migrator.up(models=self.models())
        self.assertEqual(await migrator.up(models=self.models()), [])

    async def test_models_and_a_target_are_refused(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        with self.assertRaises(ValueError):
            await migrator.up(target="a", models=self.models())

    async def test_a_blocked_generated_migration_is_not_registered(self):
        from sustained.exceptions import GuardBlocked
        from sustained.guards import BLOCK, Verdict

        def no_new_tables(statements, dialect):
            return [
                Verdict("no_new_tables", BLOCK, s)
                for s in statements
                if "async_run_users" in s
            ]

        migrator = AsyncMigrator(
            self.adapter, self.migrations(), guards=[no_new_tables]
        )
        with self.assertRaises(GuardBlocked) as caught:
            await migrator.up(models=self.models())
        self.assertEqual(caught.exception.applied, ["a"])
        self.assertNotIn("async_run_users", table_names(self.conn))
        self.assertEqual([m.id for m in migrator._migrations], ["a"])

    async def test_a_generated_drop_needs_a_rehearsal(self):
        from sustained.schema import Integer

        self.conn.execute("CREATE TABLE async_run_users (id INTEGER PRIMARY KEY)")
        self.conn.execute("CREATE TABLE spare (id INTEGER)")
        self.conn.commit()
        migrator = AsyncMigrator(self.adapter, [])
        models = self.models({"id": Integer(primary_key=True)})
        with self.assertRaises(RehearsalRequired):
            await migrator.up(models=models, allow_drops=True)
        self.assertIn("spare", table_names(self.conn))
        applied = await migrator.up(models=models, allow_drops=True, unrehearsed=True)
        self.assertEqual(len(applied), 1)
        self.assertNotIn("spare", table_names(self.conn))

    async def test_rehearse_with_models_reports_the_generated_migration(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        rehearsal = await migrator.rehearse(models=self.models())
        self.assertEqual([r.id for r in rehearsal][0], "a")
        generated = rehearsal[1]
        self.assertTrue(generated.id.startswith("auto_"))
        self.assertEqual(generated.landed, [])
        self.assertTrue(rehearsal.ok)
        self.assertTrue(rehearsal.recorded)
        self.assertNotIn("async_run_users", table_names(self.conn))
        self.assertNotIn("ta", table_names(self.conn))
        self.assertEqual(await migrator.applied(), [])

    async def test_a_rehearsal_with_models_covers_the_run_without_them(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        rehearsal = await migrator.rehearse(models=self.models())
        self.assertTrue(await migrator.rehearsed(rehearsal.key))
        pending_only = await migrator.rehearse()
        self.assertTrue(await migrator.rehearsed(pending_only.key))

    async def test_rehearse_with_models_and_nothing_pending_still_diffs(self):
        migrator = AsyncMigrator(self.adapter, [])
        rehearsal = await migrator.rehearse(models=self.models())
        self.assertEqual(len(rehearsal), 1)
        self.assertEqual(rehearsal[0].landed, [])
        self.assertNotIn("async_run_users", table_names(self.conn))

    async def test_rehearse_without_models_reports_no_landing(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        rehearsal = await migrator.rehearse()
        self.assertIsNone(rehearsal[0].landed)


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
        self.assertEqual(asked, adapter.statements)

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
