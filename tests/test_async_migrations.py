"""
Async migration runner tests using the DbApiAsyncAdapter over SQLite.
"""

import sqlite3
import unittest

from sustained.aio import DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.exceptions import RehearsalRequired
from sustained.migrations import RECEIPT_FAILED, RECEIPT_PASSED, Migration

# What a rehearsal leaves behind: the tracking table and the receipt it
# earned, both created by the rehearsal itself.
SUSTAINED_TABLES = {"sustained_migrations", "sustained_rehearsals"}


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

        schema = await async_introspect_schema(Adapter(), Dialects.POSTGRES)
        self.assertEqual(list(schema), ["shows"])
        self.assertEqual(schema["shows"].primary_key, ())

    async def test_a_failing_read_raises(self):
        from sustained.autogenerate import async_introspect_schema
        from sustained.dialects import Dialects

        with self.assertRaises(sqlite3.OperationalError):
            await async_introspect_schema(self.adapter, Dialects.POSTGRES)


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
        # A scratch rehearsal writes no receipt.
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


class TestAsyncReceipts(unittest.IsolatedAsyncioTestCase):
    """The async gate reads the same receipts the sync one writes."""

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
            await migrator.rehearsal_outcome(rehearsal.key), RECEIPT_PASSED
        )

    async def test_a_failing_rehearsal_records_the_failure(self):
        broken = Migration("001_drop", up=["DROP TABLE gate_old", "NOT SQL"])
        migrator = AsyncMigrator(self.adapter, [broken])
        rehearsal = await migrator.rehearse()
        self.assertFalse(rehearsal.ok)
        self.assertEqual(
            await migrator.rehearsal_outcome(rehearsal.key), RECEIPT_FAILED
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

    async def test_the_override_runs_without_a_receipt(self):
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
        from sustained.migrations import Migrator, receipt_key

        Migrator(self.conn, [self.drop]).record_rehearsal(receipt_key([], [self.drop]))
        migrator = AsyncMigrator(self.adapter, [self.drop])
        self.assertEqual(await migrator.up(), ["001_drop"])


if __name__ == "__main__":
    unittest.main()
