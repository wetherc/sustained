"""
The migration runner's behaviour, run against Migrator and AsyncMigrator.

Every test here is written once, in the async style, and runs twice over
an in-memory SQLite database: once on Migrator, whose methods are wrapped
to return awaitables, and once on AsyncMigrator over a DbApiAsyncAdapter.
Tests that only one runner can take stay in test_migrations.py and
test_async_migrations.py.
"""

import json
import sqlite3
import unittest
from unittest import mock

from sustained import Model
from sustained.aio import DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.exceptions import MigrationError, RehearsalRequired
from sustained.migrations import (
    REHEARSAL_FAILED,
    REHEARSAL_PASSED,
    Migration,
    Migrator,
    _legacy_checksum,
    _legacy_rehearsal_key,
    create_table_migration,
    migration_checksum,
    rehearsal_key,
)
from sustained.schema import Integer, String

# sqlite3.connect(autocommit=...) and Connection.autocommit arrived in
# Python 3.12.
HAS_SQLITE_AUTOCOMMIT = hasattr(sqlite3.Connection, "autocommit")


class MigUser(Model):
    tableName = "mig_users"
    tableColumns = {
        "id": Integer(primary_key=True),
        "email": String(120, nullable=False),
    }


# What a rehearsal leaves behind: the tracking table and the row it
# earned, both created by the rehearsal itself.
SUSTAINED_TABLES = {"sustained_migrations", "sustained_rehearsals"}


def column_names(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r[0] for r in rows}


class AwaitingMigrator:
    """
    A Migrator whose public methods return awaitables, so one test body
    drives both runners. Private attributes pass straight through, and
    so do assignments, which tests use to swap the dialect or compiler.
    """

    def __init__(self, migrator):
        object.__setattr__(self, "_migrator", migrator)

    def __getattr__(self, name):
        value = getattr(self._migrator, name)
        if name.startswith("_") or not callable(value):
            return value

        async def call(*args, **kwargs):
            return value(*args, **kwargs)

        return call

    def __setattr__(self, name, value):
        setattr(self._migrator, name, value)


class BothMigrators:
    """
    The fixture every case class shares. A variant below supplies
    migrator(), which builds the runner under test on a connection.
    """

    def setUp(self):
        super().setUp()
        self.conn = self.connect()

    def connect(self, **options):
        # The async adapter runs each call on a worker thread.
        conn = sqlite3.connect(":memory:", check_same_thread=False, **options)
        self.addCleanup(conn.close)
        return conn


class OnMigrator:
    def migrator(self, migrations, connection=None, **options):
        return AwaitingMigrator(
            Migrator(connection or self.conn, migrations, **options)
        )


class OnAsyncMigrator:
    def migrator(self, migrations, connection=None, **options):
        adapter = DbApiAsyncAdapter(connection or self.conn)
        return AsyncMigrator(adapter, migrations, **options)


class RunsCases(BothMigrators):
    def migrations(self):
        return [
            create_table_migration(MigUser),
            Migration(
                "add_flag",
                up="ALTER TABLE mig_users ADD COLUMN flag INTEGER DEFAULT 0",
                down=["ALTER TABLE mig_users DROP COLUMN flag"],
            ),
        ]

    async def test_up_applies_in_order_and_records(self):
        migrator = self.migrator(self.migrations())
        applied = await migrator.up()
        self.assertEqual(applied, ["create_mig_users", "add_flag"])
        self.assertIn("mig_users", table_names(self.conn))
        self.assertEqual(await migrator.pending(), [])
        self.assertEqual(
            await migrator.status(),
            [("create_mig_users", True), ("add_flag", True)],
        )

    async def test_up_is_idempotent(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        self.assertEqual(await migrator.up(), [])

    async def test_up_to_target(self):
        migrator = self.migrator(self.migrations())
        applied = await migrator.up(target="create_mig_users")
        self.assertEqual(applied, ["create_mig_users"])
        self.assertEqual(len(await migrator.pending()), 1)

    async def test_unknown_target_raises(self):
        migrator = self.migrator(self.migrations())
        with self.assertRaises(ValueError):
            await migrator.up(target="nope")

    async def test_up_takes_the_diff_options_by_keyword_only(self):
        migrator = self.migrator(self.migrations())
        with self.assertRaises(TypeError):
            await migrator.up(None, True, False, True)

    async def test_down_reverts_newest_first(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        reverted = await migrator.down()
        self.assertEqual(reverted, ["add_flag"])
        self.assertEqual(len(await migrator.pending()), 1)
        reverted = await migrator.down()
        self.assertEqual(reverted, ["create_mig_users"])
        self.assertNotIn("mig_users", table_names(self.conn))

    async def test_down_refuses_a_negative_step_count(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        for steps in (-1, -2):
            with self.subTest(steps=steps):
                with self.assertRaises(ValueError) as caught:
                    await migrator.down(steps=steps)
                self.assertIn("steps must be 0 or more", str(caught.exception))
        self.assertEqual(await migrator.applied(), ["create_mig_users", "add_flag"])

    async def test_down_with_zero_steps_reverts_nothing(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        self.assertEqual(await migrator.down(steps=0), [])
        self.assertEqual(await migrator.applied(), ["create_mig_users", "add_flag"])

    async def test_down_requires_down_step(self):
        migrator = self.migrator(
            [Migration("one_way", up="CREATE TABLE ow (id INTEGER)")]
        )
        await migrator.up()
        with self.assertRaises(ValueError):
            await migrator.down()

    async def test_down_refuses_a_migration_edited_after_it_applied(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        edited = self.migrations()
        edited[1] = Migration(
            "add_flag",
            up="ALTER TABLE mig_users ADD COLUMN flag INTEGER DEFAULT 1",
            down=["ALTER TABLE mig_users DROP COLUMN flag"],
        )
        later = self.migrator(edited)
        with self.assertRaises(MigrationError) as caught:
            await later.down()
        message = str(caught.exception)
        self.assertIn("changed after it was applied", message)
        self.assertIn("allow_changed=True", message)
        self.assertEqual(await later.applied(), ["create_mig_users", "add_flag"])
        # The flag says the caller knows, so the revert runs.
        self.assertEqual(await later.down(allow_changed=True), ["add_flag"])

    async def test_down_reverts_nothing_when_an_older_migration_changed(self):
        migrations = self.migrations() + [
            Migration(
                "add_note",
                up="ALTER TABLE mig_users ADD COLUMN note TEXT",
                down=["ALTER TABLE mig_users DROP COLUMN note"],
            )
        ]
        await self.migrator(migrations).up()
        edited = list(migrations)
        edited[1] = Migration(
            "add_flag",
            up="ALTER TABLE mig_users ADD COLUMN flag INTEGER DEFAULT 1",
            down=["ALTER TABLE mig_users DROP COLUMN flag"],
        )
        later = self.migrator(edited)
        with self.assertRaises(MigrationError):
            await later.down(steps=2)
        self.assertEqual(
            await later.applied(), ["create_mig_users", "add_flag", "add_note"]
        )
        self.assertIn("note", column_names(self.conn, "mig_users"))

    async def test_down_reverts_nothing_when_an_older_migration_has_no_down_step(self):
        migrations = [
            create_table_migration(MigUser),
            Migration("one_way", up="CREATE TABLE ow (id INTEGER)"),
            Migration(
                "add_note",
                up="ALTER TABLE mig_users ADD COLUMN note TEXT",
                down=["ALTER TABLE mig_users DROP COLUMN note"],
            ),
        ]
        migrator = self.migrator(migrations)
        await migrator.up()
        with self.assertRaises(ValueError):
            await migrator.down(steps=2)
        self.assertEqual(
            await migrator.applied(), ["create_mig_users", "one_way", "add_note"]
        )
        self.assertIn("note", column_names(self.conn, "mig_users"))

    async def test_down_to_carries_the_changed_flag(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        edited = self.migrations()
        edited[1] = Migration(
            "add_flag",
            up="ALTER TABLE mig_users ADD COLUMN flag INTEGER DEFAULT 1",
            down=["ALTER TABLE mig_users DROP COLUMN flag"],
        )
        later = self.migrator(edited)
        with self.assertRaises(MigrationError):
            await later.down_to("create_mig_users")
        self.assertEqual(
            await later.down_to("create_mig_users", allow_changed=True), ["add_flag"]
        )

    async def test_down_accepts_a_migration_whose_checksum_matches(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        self.assertEqual(await migrator.down(), ["add_flag"])

    async def test_down_requires_registered_migration(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        stripped = self.migrator([])
        with self.assertRaises(ValueError):
            await stripped.down()

    async def test_failed_migration_rolls_back_tracking(self):
        migrations = [
            Migration(
                "ok", up="CREATE TABLE ok_tbl (id INTEGER)", down="DROP TABLE ok_tbl"
            ),
            Migration("boom", up="THIS IS NOT SQL"),
        ]
        migrator = self.migrator(migrations)
        with self.assertRaises(sqlite3.OperationalError):
            await migrator.up()
        self.assertEqual(await migrator.applied(), ["ok"])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            self.migrator(
                [Migration("a", up="SELECT 1"), Migration("a", up="SELECT 1")],
            )


class TrackingTableCases(BothMigrators):
    def columns(self):
        rows = self.conn.execute("PRAGMA table_info(sustained_migrations)").fetchall()
        return {r[1] for r in rows}

    def rows(self):
        return self.conn.execute(
            "SELECT id, seq, checksum, applied_at, execution_ms, success "
            "FROM sustained_migrations ORDER BY seq"
        ).fetchall()

    async def test_fresh_table_has_full_shape(self):
        await self.migrator([]).up()
        self.assertEqual(
            self.columns(),
            {
                "id",
                "seq",
                "checksum",
                "applied_at",
                "execution_ms",
                "success",
                "generated",
                "steps",
            },
        )

    async def test_apply_records_checksum_seq_timing_and_success(self):
        migration = Migration("one", up="CREATE TABLE t1 (id INTEGER)")
        await self.migrator([migration]).up()
        (row,) = self.rows()
        self.assertEqual(row[0], "one")
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], migration_checksum(migration))
        self.assertIsInstance(row[4], int)
        self.assertGreaterEqual(row[4], 0)
        self.assertTrue(row[5])

    async def test_seq_increments_across_runs(self):
        first = self.migrator([Migration("a", up="CREATE TABLE a1 (x INTEGER)")])
        await first.up()
        second = self.migrator(
            [
                Migration("a", up="CREATE TABLE a1 (x INTEGER)"),
                Migration("b", up="CREATE TABLE b1 (x INTEGER)"),
            ],
        )
        await second.up()
        self.assertEqual([(r[0], r[1]) for r in self.rows()], [("a", 1), ("b", 2)])

    async def test_legacy_tracking_table_is_upgraded_in_place(self):
        self.conn.execute(
            "CREATE TABLE sustained_migrations "
            "(id VARCHAR(255) PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "INSERT INTO sustained_migrations VALUES "
            "('old_one', '2024-01-01T00:00:00'), ('old_two', '2024-02-01T00:00:00')"
        )
        self.conn.commit()
        migrator = self.migrator(
            [Migration("old_one", up="SELECT 1"), Migration("old_two", up="SELECT 1")],
        )
        self.assertEqual(await migrator.applied(), ["old_one", "old_two"])
        self.assertIn("seq", self.columns())
        self.assertEqual(
            [(r[0], r[1], r[5]) for r in self.rows()],
            [("old_one", 1, 1), ("old_two", 2, 1)],
        )
        self.assertEqual(await migrator.up(), [])

    async def test_partial_upgrade_keeps_existing_success_values(self):
        self.conn.execute(
            "CREATE TABLE sustained_migrations "
            "(id VARCHAR(255) PRIMARY KEY, applied_at TEXT NOT NULL, "
            "success BOOLEAN)"
        )
        self.conn.execute(
            "INSERT INTO sustained_migrations VALUES "
            "('good', '2024-01-01T00:00:00', 1), "
            "('bad', '2024-02-01T00:00:00', 0)"
        )
        self.conn.commit()
        migrator = self.migrator([Migration("good", up="SELECT 1")])
        self.assertEqual(await migrator.applied(), ["good"])
        stored = dict(
            self.conn.execute("SELECT id, success FROM sustained_migrations").fetchall()
        )
        self.assertEqual(stored, {"good": 1, "bad": 0})

    async def test_script_up_renders_full_bookkeeping_row(self):
        migration = Migration("one", up="CREATE TABLE t1 (id INTEGER)")
        script = await self.migrator([migration]).script("up")
        self.assertIn("(id, seq, checksum, applied_at, execution_ms, success)", script)
        self.assertIn(f"'{migration_checksum(migration)}'", script)
        self.assertIn("1, ", script)
        self.assertIn("TRUE", script)


class ValidateAndRepairCases(BothMigrators):
    async def test_validate_passes_on_a_clean_history(self):
        migrator = self.migrator([Migration("a", up="CREATE TABLE va (x INTEGER)")])
        await migrator.up()
        self.assertEqual(await migrator.validate(), [])

    async def test_validate_detects_an_edited_migration(self):
        await self.migrator([Migration("a", up="CREATE TABLE va (x INTEGER)")]).up()
        edited = self.migrator([Migration("a", up="CREATE TABLE va (x BIGINT)")])
        with self.assertRaises(MigrationError):
            await edited.validate()
        problems = await edited.validate(raise_on_problems=False)
        self.assertEqual(len(problems), 1)
        self.assertIn("checksum mismatch", problems[0])

    async def test_validate_detects_an_unregistered_applied_migration(self):
        await self.migrator([Migration("a", up="CREATE TABLE va (x INTEGER)")]).up()
        problems = await self.migrator([]).validate(raise_on_problems=False)
        self.assertIn("not registered", problems[0])

    async def test_up_refuses_an_edited_migration_unless_told_not_to_validate(self):
        await self.migrator([Migration("a", up="CREATE TABLE va (x INTEGER)")]).up()
        edited = self.migrator(
            [
                Migration("a", up="CREATE TABLE va (x BIGINT)"),
                Migration("b", up="CREATE TABLE vb (x INTEGER)"),
            ],
        )
        with self.assertRaises(MigrationError):
            await edited.up()
        self.assertEqual(await edited.up(validate=False), ["b"])

    async def test_out_of_order_pending_migration_is_refused_by_default(self):
        await self.migrator(
            [
                Migration("a", up="CREATE TABLE oa (x INTEGER)"),
                Migration("c", up="CREATE TABLE oc (x INTEGER)"),
            ],
        ).up()
        late = self.migrator(
            [
                Migration("a", up="CREATE TABLE oa (x INTEGER)"),
                Migration("b", up="CREATE TABLE ob (x INTEGER)"),
                Migration("c", up="CREATE TABLE oc (x INTEGER)"),
            ],
        )
        with self.assertRaises(MigrationError):
            await late.up()
        self.assertEqual(await late.up(allow_out_of_order=True), ["b"])
        self.assertEqual(await late.validate(), [])

    async def test_repair_accepts_an_edited_migration(self):
        await self.migrator([Migration("a", up="CREATE TABLE va (x INTEGER)")]).up()
        edited = self.migrator([Migration("a", up="CREATE TABLE va (x BIGINT)")])
        actions = await edited.repair()
        self.assertEqual(actions, ["updated the stored checksum of 'a'"])
        self.assertEqual(await edited.validate(), [])

    async def test_repair_leaves_a_changed_repeatable_pending(self):
        await self.migrator(
            [Migration("r", up="CREATE VIEW rv AS SELECT 1", repeatable=True)],
        ).up()
        changed = self.migrator(
            [Migration("r", up="CREATE VIEW rv2 AS SELECT 2", repeatable=True)],
        )
        self.assertEqual(await changed.repair(), [])
        self.assertEqual([m.id for m in await changed.pending()], ["r"])
        self.assertEqual(await changed.up(), ["r"])

    def store_legacy_checksum(self, migration):
        self.conn.execute(
            "UPDATE sustained_migrations SET checksum = ? WHERE id = ?",
            (_legacy_checksum(migration), migration.id),
        )
        self.conn.commit()

    async def test_a_row_with_the_legacy_checksum_still_matches(self):
        migration = Migration(
            "a", up=["CREATE TABLE va (x INTEGER)", "SELECT 1"], down="DROP TABLE va"
        )
        repeatable = Migration("r", up="CREATE VIEW rv AS SELECT 1", repeatable=True)
        await self.migrator([migration, repeatable]).up()
        self.store_legacy_checksum(migration)
        self.store_legacy_checksum(repeatable)
        migrator = self.migrator([migration, repeatable])
        self.assertEqual(await migrator.validate(), [])
        self.assertEqual(await migrator.pending(), [])
        self.assertEqual(
            await migrator.statuses(), [("a", "applied"), ("r", "applied")]
        )
        self.assertEqual(await migrator.down(), ["a"])

    async def test_a_split_statement_reads_as_an_edit(self):
        await self.migrator(
            [Migration("a", up=["CREATE TABLE va (x INTEGER,\ny INTEGER)"])]
        ).up()
        split = self.migrator(
            [Migration("a", up=["CREATE TABLE va (x INTEGER,", "y INTEGER)"])],
        )
        problems = await split.validate(raise_on_problems=False)
        self.assertEqual(len(problems), 1)
        self.assertIn("checksum mismatch", problems[0])

    async def test_a_legacy_row_still_reads_an_edit(self):
        migration = Migration("a", up="CREATE TABLE va (x INTEGER)")
        await self.migrator([migration]).up()
        self.store_legacy_checksum(migration)
        edited = self.migrator([Migration("a", up="CREATE TABLE va (x BIGINT)")])
        self.assertEqual(len(await edited.validate(raise_on_problems=False)), 1)

    async def test_repair_rewrites_a_legacy_checksum_in_the_current_format(self):
        migration = Migration("a", up="CREATE TABLE va (x INTEGER)")
        await self.migrator([migration]).up()
        self.store_legacy_checksum(migration)
        migrator = self.migrator([migration])
        self.assertEqual(
            await migrator.repair(), ["updated the checksum format of 'a'"]
        )
        self.assertEqual(
            (await migrator.applied_records())[0].checksum,
            migration_checksum(migration),
        )
        self.assertEqual(await migrator.repair(), [])

    async def test_repair_rewrites_a_legacy_repeatable_and_keeps_it_applied(self):
        repeatable = Migration("r", up="CREATE VIEW rv AS SELECT 1", repeatable=True)
        await self.migrator([repeatable]).up()
        self.store_legacy_checksum(repeatable)
        migrator = self.migrator([repeatable])
        self.assertEqual(
            await migrator.repair(), ["updated the checksum format of 'r'"]
        )
        self.assertEqual(
            (await migrator.applied_records())[0].checksum,
            migration_checksum(repeatable),
        )
        self.assertEqual(await migrator.pending(), [])

    async def test_repair_leaves_a_changed_legacy_repeatable_pending(self):
        await self.migrator(
            [Migration("r", up="CREATE VIEW rv AS SELECT 1", repeatable=True)],
        ).up()
        self.store_legacy_checksum(
            Migration("r", up="CREATE VIEW rv AS SELECT 1", repeatable=True)
        )
        changed = self.migrator(
            [Migration("r", up="CREATE VIEW rv2 AS SELECT 2", repeatable=True)],
        )
        self.assertEqual(await changed.repair(), [])
        self.assertEqual([m.id for m in await changed.pending()], ["r"])

    async def test_repair_adopts_legacy_rows_without_checksums(self):
        self.conn.execute(
            "CREATE TABLE sustained_migrations "
            "(id VARCHAR(255) PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "INSERT INTO sustained_migrations VALUES ('a', '2024-01-01T00:00:00')"
        )
        self.conn.commit()
        migration = Migration("a", up="CREATE TABLE va (x INTEGER)")
        migrator = self.migrator([migration])
        self.assertEqual(
            await migrator.repair(), ["updated the stored checksum of 'a'"]
        )
        stored = self.conn.execute(
            "SELECT checksum FROM sustained_migrations WHERE id = 'a'"
        ).fetchone()[0]
        self.assertEqual(stored, migration_checksum(migration))


class NonTransactionalMigrationsCases(BothMigrators):
    async def test_it_applies_and_records_its_row(self):
        migrator = self.migrator(
            [
                Migration(
                    "nt",
                    up="CREATE TABLE nt (x INTEGER)",
                    down="DROP TABLE nt",
                    transactional=False,
                )
            ],
        )
        self.assertEqual(await migrator.up(), ["nt"])
        self.assertEqual(await migrator.applied(), ["nt"])
        self.assertIn("nt", table_names(self.conn))

    async def test_it_reverts(self):
        migrator = self.migrator(
            [
                Migration(
                    "nt",
                    up="CREATE TABLE nt (x INTEGER)",
                    down="DROP TABLE nt",
                    transactional=False,
                )
            ],
        )
        await migrator.up()
        self.assertEqual(await migrator.down(), ["nt"])
        self.assertNotIn("nt", table_names(self.conn))

    async def test_a_failure_leaves_the_earlier_statements_and_a_row(self):
        migrator = self.migrator(
            [
                Migration(
                    "nt",
                    up=["CREATE TABLE nt (x INTEGER)", "THIS IS NOT SQL"],
                    transactional=False,
                )
            ],
        )
        with self.assertRaises(sqlite3.OperationalError):
            await migrator.up()
        self.assertIn("nt", table_names(self.conn))
        row = self.conn.execute(
            "SELECT id, success FROM sustained_migrations"
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("nt", 0))
        self.assertEqual(await migrator.applied(), [])

    async def test_a_rehearsal_leaves_it_out_and_reports_it_unproved(self):
        # The rehearsal runs inside one transaction, which the statements
        # of a transactional=False migration refuse or ignore. Running it
        # there would fail a migration a real up() applies, so it is left
        # out and its result says nothing was proved.
        migrator = self.migrator(
            [
                Migration(
                    "first",
                    up="CREATE TABLE nt_first (x INTEGER)",
                    down="DROP TABLE nt_first",
                ),
                Migration(
                    "nt",
                    up="CREATE TABLE nt (x INTEGER)",
                    down="DROP TABLE nt",
                    transactional=False,
                ),
            ],
        )
        rehearsal = await migrator.rehearse()
        self.assertTrue(rehearsal.ok)
        by_id = {r.id: r for r in rehearsal}
        self.assertTrue(by_id["first"].up_ok)
        self.assertIsNone(by_id["nt"].up_ok)
        self.assertIsNone(by_id["nt"].down_ok)
        self.assertIn("outside a transaction", by_id["nt"].error)
        self.assertNotIn("nt", table_names(self.conn))
        self.assertNotIn("nt_first", table_names(self.conn))


class BaselineCases(BothMigrators):
    def migrations(self):
        return [
            create_table_migration(MigUser),
            Migration(
                "add_flag",
                up="ALTER TABLE mig_users ADD COLUMN flag INTEGER DEFAULT 0",
                down="ALTER TABLE mig_users DROP COLUMN flag",
            ),
        ]

    async def test_baseline_records_without_running(self):
        migrations = self.migrations()
        migrator = self.migrator(migrations)
        recorded = await migrator.baseline("create_mig_users")
        self.assertEqual(recorded, ["create_mig_users"])
        self.assertNotIn("mig_users", table_names(self.conn))
        row = self.conn.execute(
            "SELECT id, seq, checksum, execution_ms, success "
            "FROM sustained_migrations"
        ).fetchone()
        self.assertEqual(row[0], "create_mig_users")
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], migration_checksum(migrations[0]))
        self.assertIsNone(row[3])
        self.assertEqual(row[4], 1)

    async def test_baseline_then_up_applies_only_the_rest(self):
        self.conn.executescript(
            "CREATE TABLE mig_users (id INTEGER PRIMARY KEY, "
            "email VARCHAR(120) NOT NULL)"
        )
        migrator = self.migrator(self.migrations())
        await migrator.baseline("create_mig_users")
        self.assertEqual(await migrator.validate(), [])
        self.assertEqual(await migrator.up(), ["add_flag"])
        self.assertEqual(await migrator.applied(), ["create_mig_users", "add_flag"])

    async def test_baseline_skips_already_applied(self):
        migrator = self.migrator(self.migrations())
        await migrator.up(target="create_mig_users")
        self.assertEqual(await migrator.baseline("add_flag"), ["add_flag"])
        seqs = [r.seq for r in await migrator.applied_records()]
        self.assertEqual(seqs, [1, 2])

    async def test_baseline_unknown_target_raises(self):
        migrator = self.migrator(self.migrations())
        with self.assertRaises(ValueError):
            await migrator.baseline("nope")


class PlanCases(BothMigrators):
    async def test_plan_returns_migration_without_touching_anything(self):
        migrator = self.migrator([])
        migration = await migrator.plan([MigUser], migration_id="planned")
        self.assertEqual(migration.id, "planned")
        self.assertTrue(any("CREATE TABLE" in s for s in migration.up))
        self.assertNotIn("mig_users", table_names(self.conn))
        self.assertEqual(await migrator.status(), [])

    async def test_plan_returns_none_when_schema_is_current(self):
        migrator = self.migrator([])
        await migrator.up(models=[MigUser])
        self.assertIsNone(await migrator.plan([MigUser]))


class RepeatableMigrationsCases(BothMigrators):
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
        migrator = self.migrator(self.migrations())
        self.assertEqual(await migrator.up(), ["001_t", "active_view"])
        self.assertEqual(await migrator.up(), [])
        records = {r.id: r for r in await migrator.applied_records()}
        self.assertEqual(records["active_view"].seq, 2)

    async def test_changed_checksum_reruns_and_updates_in_place(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        first_seq = {r.id: r.seq for r in await migrator.applied_records()}
        self.conn.execute("DROP VIEW v")
        changed = self.migrator(self.migrations("SELECT id, id AS b FROM t"))
        self.assertEqual(await changed.up(), ["active_view"])
        records = {r.id: r for r in await changed.applied_records()}
        self.assertEqual(records["active_view"].seq, first_seq["active_view"])
        self.assertEqual(len(await changed.applied_records()), 2)

    async def test_changed_checksum_is_not_a_validation_problem(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        changed = self.migrator(self.migrations("SELECT id, id AS b FROM t"))
        self.assertEqual(await changed.validate(), [])

    async def test_statuses_reports_changed(self):
        migrator = self.migrator(self.migrations())
        self.assertEqual(
            await migrator.statuses(),
            [("001_t", "pending"), ("active_view", "pending")],
        )
        await migrator.up()
        self.assertEqual(
            await migrator.statuses(),
            [("001_t", "applied"), ("active_view", "applied")],
        )
        changed = self.migrator(self.migrations("SELECT id, id AS b FROM t"))
        self.assertEqual(
            await changed.statuses(),
            [("001_t", "applied"), ("active_view", "changed")],
        )

    async def test_pending_includes_changed_repeatable(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        changed = self.migrator(self.migrations("SELECT id, id AS b FROM t"))
        self.assertEqual([m.id for m in await changed.pending()], ["active_view"])

    async def test_down_skips_repeatables(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        self.assertEqual(await migrator.down(), ["001_t"])
        applied = {r.id for r in await migrator.applied_records() if r.success}
        self.assertEqual(applied, {"active_view"})

    async def test_down_to_skips_repeatables(self):
        migrations = self.migrations()
        migrations.insert(
            1,
            Migration("002_u", up="CREATE TABLE u (id INTEGER)", down="DROP TABLE u"),
        )
        migrator = self.migrator(migrations)
        await migrator.up()
        self.assertEqual(await migrator.down_to("001_t"), ["002_u"])

    async def test_target_skips_repeatables_and_rejects_repeatable_target(self):
        migrations = self.migrations()
        migrations.insert(
            1,
            Migration("002_u", up="CREATE TABLE u (id INTEGER)", down="DROP TABLE u"),
        )
        migrator = self.migrator(migrations)
        self.assertEqual(await migrator.up(target="001_t"), ["001_t"])
        with self.assertRaisesRegex(ValueError, "repeatable"):
            await migrator.up(target="active_view")
        self.assertEqual(await migrator.up(), ["002_u", "active_view"])

    async def test_baseline_records_repeatables_at_current_checksum(self):
        self.conn.execute("CREATE TABLE t (id INTEGER)")
        self.conn.execute("CREATE VIEW v AS SELECT id FROM t")
        migrator = self.migrator(self.migrations())
        self.assertEqual(await migrator.baseline("001_t"), ["001_t", "active_view"])
        self.assertEqual(await migrator.up(), [])
        with self.assertRaisesRegex(ValueError, "repeatable"):
            await migrator.baseline("active_view")

    async def test_script_up_renders_insert_then_update(self):
        migrator = self.migrator(self.migrations())
        script = await migrator.script("up")
        self.assertIn("-- repeat: active_view", script)
        self.assertIn("INSERT INTO", script)
        await migrator.up()
        changed = self.migrator(self.migrations("SELECT id, id AS b FROM t"))
        script = await changed.script("up")
        self.assertIn("-- repeat: active_view", script)
        self.assertIn("UPDATE", script)
        self.assertNotIn("-- up:", script)

    async def test_script_down_skips_repeatables(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        script = await migrator.script("down")
        self.assertNotIn("active_view", script)
        self.assertIn("001_t", script)

    async def test_out_of_order_check_ignores_repeatables(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        migrations = self.migrations()
        migrations.append(
            Migration("002_u", up="CREATE TABLE u (id INTEGER)", down="DROP TABLE u")
        )
        later = self.migrator(migrations)
        self.assertEqual(await later.validate(), [])
        self.assertEqual(await later.up(), ["002_u"])

    async def test_failed_repeatable_rerun_updates_failure_row(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        broken = self.migrator(
            [
                self.migrations()[0],
                Migration("active_view", up="SELECT * FROM missing", repeatable=True),
            ],
        )
        with mock.patch.object(
            broken._compiler, "supports_transactions", return_value=False
        ):
            with self.assertRaises(sqlite3.OperationalError):
                await broken.up(validate=False)
        records = {r.id: r for r in await broken.applied_records()}
        self.assertFalse(records["active_view"].success)
        self.assertEqual(len(await broken.applied_records()), 2)


class ReadOnlyPathsCases(BothMigrators):
    """
    The paths that only report on a run leave the database alone, so a
    review on a production connection writes nothing.
    """

    def migrations(self):
        return [
            Migration(
                "001_t", up="CREATE TABLE ro_t (id INTEGER)", down="DROP TABLE ro_t"
            )
        ]

    async def test_script_creates_no_tracking_table(self):
        migrator = self.migrator(self.migrations())
        for direction in ("up", "down"):
            with self.subTest(direction=direction):
                await migrator.script(direction)
                self.assertNotIn("sustained_migrations", table_names(self.conn))

    async def test_plan_style_reads_create_no_tracking_table(self):
        migrator = self.migrator(self.migrations())
        self.assertEqual([m.id for m in await migrator.pending()], ["001_t"])
        self.assertEqual(await migrator.status(), [("001_t", False)])
        self.assertEqual(await migrator.statuses(), [("001_t", "pending")])
        self.assertEqual(await migrator.validate(), [])
        self.assertEqual(await migrator.read_applied_records(), [])
        self.assertEqual(await migrator.read_applied(), [])
        self.assertNotIn("sustained_migrations", table_names(self.conn))

    async def test_a_read_reports_the_rows_once_the_table_is_there(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        self.assertEqual(await migrator.read_applied(), ["001_t"])
        self.assertIn("-- down: 001_t", await migrator.script("down"))
        # A fresh migrator has not created the table itself, so the read
        # goes through the probe rather than the ready flag.
        later = self.migrator(self.migrations())
        self.assertEqual([r.id for r in await later.read_applied_records()], ["001_t"])

    async def test_a_tracking_table_of_the_old_shape_reads_as_empty(self):
        self.conn.execute("CREATE TABLE sustained_migrations (id TEXT)")
        self.conn.execute("INSERT INTO sustained_migrations VALUES ('001_t')")
        migrator = self.migrator(self.migrations())
        self.assertEqual(await migrator.read_applied_records(), [])

    async def test_up_still_creates_the_tracking_table(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        self.assertIn("sustained_migrations", table_names(self.conn))
        self.assertEqual([r.id for r in await migrator.applied_records()], ["001_t"])


class RehearseCases(BothMigrators):
    """Rehearsals run everything and leave the database as they found it."""

    def migrations(self):
        return [
            Migration(
                "001_users",
                up="CREATE TABLE r_users (id INTEGER)",
                down="DROP TABLE r_users",
            ),
            Migration(
                "002_flags",
                up=[
                    "CREATE TABLE r_flags (id INTEGER)",
                    "CREATE TABLE r_more (id INTEGER)",
                ],
                down=["DROP TABLE r_more", "DROP TABLE r_flags"],
            ),
            Migration("r_view", up="CREATE VIEW r_v AS SELECT 1", repeatable=True),
        ]

    async def test_rehearse_proves_both_directions_and_changes_nothing(self):
        migrator = self.migrator(self.migrations())
        results = await migrator.rehearse()
        self.assertEqual(
            [(r.id, r.up_ok, r.down_ok) for r in results],
            [
                ("001_users", True, True),
                ("002_flags", True, True),
                ("r_view", True, None),
            ],
        )
        self.assertEqual(results[2].error, "no down step (repeatable)")
        self.assertEqual(table_names(self.conn), SUSTAINED_TABLES)
        self.assertEqual(await migrator.applied_records(), [])
        self.assertEqual(len(await migrator.pending()), 3)

    async def test_rehearse_after_a_partial_run_covers_only_the_rest(self):
        migrator = self.migrator(self.migrations())
        # A targeted run skips the repeatables, so 002 and the view remain.
        await migrator.up(target="001_users")
        results = await migrator.rehearse()
        self.assertEqual(
            [(r.id, r.down_ok) for r in results],
            [("002_flags", True), ("r_view", None)],
        )
        self.assertIn("r_users", table_names(self.conn))
        self.assertNotIn("r_flags", table_names(self.conn))
        self.assertEqual(await migrator.applied(), ["001_users"])

    async def test_rehearse_reruns_a_changed_repeatable_without_recording_it(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        before = {r.id: r.checksum for r in await migrator.applied_records()}
        changed = self.migrations()[:2] + [
            Migration("r_view", up="CREATE VIEW r_v2 AS SELECT 2", repeatable=True)
        ]
        later = self.migrator(changed)
        results = await later.rehearse()
        self.assertEqual([r.id for r in results], ["r_view"])
        self.assertTrue(results[0].up_ok)
        after = {r.id: r.checksum for r in await later.applied_records()}
        self.assertEqual(before, after)

    async def test_nothing_pending_rehearses_nothing(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        self.assertEqual(await migrator.rehearse(), [])

    async def test_failing_up_step_stops_the_rehearsal(self):
        migrations = [
            self.migrations()[0],
            Migration("002_bad", up="CRATE TABLE oops", down="DROP TABLE oops"),
            Migration("003_never", up="CREATE TABLE r_never (id INTEGER)"),
        ]
        results = await self.migrator(migrations).rehearse()
        self.assertEqual(
            [(r.id, r.up_ok) for r in results],
            [("001_users", True), ("002_bad", False)],
        )
        self.assertIn("syntax error", results[1].error)
        self.assertEqual(results[0].error, "down not rehearsed: the run stopped")
        self.assertEqual(table_names(self.conn), SUSTAINED_TABLES)

    async def test_missing_down_step_stops_the_down_sweep(self):
        migrations = [
            self.migrations()[0],
            Migration("002_forward", up="CREATE TABLE r_fwd (id INTEGER)"),
        ]
        results = await self.migrator(migrations).rehearse()
        self.assertEqual([r.down_ok for r in results], [None, None])
        self.assertEqual(results[1].error, "no down step")
        self.assertEqual(
            results[0].error, "down not reached: '002_forward' has no down step"
        )

    async def test_failing_down_step_is_reported_and_stops_the_sweep(self):
        migrations = [
            self.migrations()[0],
            Migration(
                "002_bad_down",
                up="CREATE TABLE r_bad (id INTEGER)",
                down="DROP TABLE r_missing",
            ),
        ]
        results = await self.migrator(migrations).rehearse()
        self.assertEqual(
            [(r.id, r.down_ok) for r in results][1], ("002_bad_down", False)
        )
        self.assertIn("r_missing", results[1].error)
        self.assertEqual(
            results[0].error, "down not reached: '002_bad_down' down failed"
        )
        self.assertEqual(table_names(self.conn), SUSTAINED_TABLES)

    async def test_validation_problems_stop_the_rehearsal(self):
        migrator = self.migrator(self.migrations())
        await migrator.up()
        edited = [
            Migration(
                "001_users",
                up="CREATE TABLE r_users (id INTEGER, extra TEXT)",
                down="DROP TABLE r_users",
            )
        ] + self.migrations()[1:]
        with self.assertRaises(MigrationError):
            await self.migrator(edited).rehearse()

    async def test_rehearse_refuses_a_dialect_that_cannot_roll_back(self):
        from sustained.dialects import Dialects

        migrator = self.migrator(self.migrations(), dialect=Dialects.ATHENA)
        with self.assertRaises(ValueError) as caught:
            await migrator.rehearse()
        self.assertIn("athena is not on that list", str(caught.exception))
        self.assertIn("get_rehearsal_connection()", str(caught.exception))

    async def test_scratch_waives_the_dialect_check(self):
        from sustained.dialects import Dialects

        # The dialect drives the check; the compiler stays SQLite's so the
        # statements still run here.
        migrator = self.migrator([self.migrations()[0]])
        migrator._dialect = Dialects.MSSQL
        with self.assertRaises(ValueError):
            await migrator.rehearse()
        results = await migrator.rehearse(scratch=True)
        self.assertEqual(
            [(r.id, r.up_ok, r.down_ok) for r in results], [("001_users", True, True)]
        )
        # A scratch rehearsal writes no row: it belongs on the
        # database the next run will read, not on the throwaway one.
        self.assertEqual(table_names(self.conn), {"sustained_migrations"})
        self.assertFalse(results.recorded)

    @unittest.skipUnless(HAS_SQLITE_AUTOCOMMIT, "sqlite3 autocommit needs 3.12")
    async def test_rehearse_refuses_an_autocommit_connection(self):
        conn = self.connect(autocommit=True)
        with self.assertRaises(ValueError) as caught:
            await self.migrator(self.migrations(), connection=conn).rehearse()
        self.assertIn("autocommit", str(caught.exception))

    async def test_rehearsal_writes_no_failure_row_without_transactions(self):
        migrations = [Migration("002_bad", up="CRATE TABLE oops")]
        migrator = self.migrator(migrations)
        with mock.patch.object(
            migrator._compiler, "supports_transactions", return_value=False
        ):
            results = await migrator.rehearse()
        self.assertFalse(results[0].up_ok)
        self.assertEqual(await migrator.applied_records(), [])


class GeneratedRowsCases(BothMigrators):
    """
    A migration generated from the models is recorded as generated, so a
    later migrator does not report an id nothing on disk carries.
    """

    def models(self):
        return [
            type(
                "GenUser",
                (Model,),
                {
                    "tableName": "gen_users",
                    "tableColumns": {"id": Integer(primary_key=True)},
                },
            )
        ]

    async def test_a_generated_row_is_marked_and_validates_elsewhere(self):
        await self.migrator([]).up(models=self.models())
        record = (await self.migrator([]).applied_records())[0]
        self.assertTrue(record.generated)
        self.assertEqual(await self.migrator([]).validate(), [])

    async def test_a_later_process_can_revert_a_generated_migration(self):
        await self.migrator([]).up(models=self.models())
        self.assertIn("gen_users", table_names(self.conn))
        # A migrator that never saw the diff: the statements come off the
        # tracking row.
        later = self.migrator([])
        applied_id = (await later.applied())[0]
        self.assertEqual(await later.down(), [applied_id])
        self.assertNotIn("gen_users", table_names(self.conn))
        self.assertEqual(await later.applied(), [])

    async def test_a_down_script_reverts_a_generated_migration_from_its_row(self):
        registered = Migration(
            "001_base", up="CREATE TABLE base (id INTEGER)", down="DROP TABLE base"
        )
        await self.migrator([registered]).up(models=self.models())
        later = self.migrator([registered])
        generated_id = (await later.applied())[-1]
        script = await later.script("down")
        # The generated migration comes first, newest-first, and the
        # script carries on past it to the registered one.
        self.assertIn(f"-- down: {generated_id}\nDROP TABLE", script)
        self.assertNotIn("stopping", script)
        self.assertLess(
            script.index(f"-- down: {generated_id}"), script.index("-- down: 001_base")
        )

    async def test_a_down_script_stops_at_a_generated_row_without_steps(self):
        await self.migrator([]).up(models=self.models())
        generated_id = (await self.migrator([]).applied())[0]
        self.conn.execute("UPDATE sustained_migrations SET steps = NULL")
        self.assertEqual(
            await self.migrator([]).script("down"),
            f"-- down: {generated_id} has no reversible step; stopping",
        )

    async def test_a_generated_migration_without_a_down_step_still_refuses(self):
        await self.migrator([]).up(models=self.models())
        applied_id = (await self.migrator([]).applied())[0]
        self.conn.execute(
            "UPDATE sustained_migrations SET steps = ? WHERE id = ?",
            ('{"up": ["SELECT 1"], "down": null}', applied_id),
        )
        with self.assertRaises(ValueError) as caught:
            await self.migrator([]).down()
        self.assertIn("has no down step", str(caught.exception))

    async def test_a_row_with_unreadable_steps_is_not_revertible(self):
        await self.migrator([]).up(models=self.models())
        self.conn.execute("UPDATE sustained_migrations SET steps = 'not json'")
        with self.assertRaises(ValueError) as caught:
            await self.migrator([]).down()
        self.assertIn("not registered", str(caught.exception))

    async def test_a_registered_migration_stores_no_steps(self):
        await self.migrator(
            [Migration("001_t", up="CREATE TABLE gt (id INTEGER)")]
        ).up()
        (row,) = self.conn.execute("SELECT steps FROM sustained_migrations").fetchall()
        self.assertIsNone(row[0])

    async def test_a_registered_migration_is_not_marked(self):
        migrator = self.migrator(
            [Migration("001_t", up="CREATE TABLE gt (id INTEGER)")]
        )
        await migrator.up()
        self.assertFalse((await migrator.applied_records())[0].generated)
        self.assertEqual(
            await self.migrator([]).validate(raise_on_problems=False),
            ["applied migration '001_t' is not registered with this migrator"],
        )


class RehearsalProofsCases(BothMigrators):
    """
    A rehearsal reports what the schema said: whether the models landed,
    and whether the down steps put the schema back.
    """

    def models(self, **columns):
        model = type(
            "RpUser",
            (Model,),
            {
                "tableName": "rp_users",
                "tableColumns": {
                    "id": Integer(primary_key=True),
                    "email": String(120),
                    **columns,
                },
            },
        )
        return [model]

    async def test_a_clean_sweep_proves_the_schema_came_back(self):
        migrator = self.migrator(
            [
                Migration(
                    "001_t",
                    up="CREATE TABLE rp_t (id INTEGER)",
                    down="DROP TABLE rp_t",
                )
            ],
        )
        results = await migrator.rehearse()
        self.assertEqual(results[0].reversed, [])
        self.assertIsNone(results[0].landed)

    async def test_a_down_step_that_leaves_an_object_behind_is_reported(self):
        migrator = self.migrator(
            [
                Migration(
                    "001_t",
                    up=[
                        "CREATE TABLE rp_t (id INTEGER)",
                        "CREATE TABLE rp_leftover (id INTEGER)",
                    ],
                    down="DROP TABLE rp_t",
                )
            ],
        )
        results = await migrator.rehearse()
        self.assertTrue(results[0].down_ok)
        self.assertEqual(results[0].reversed, ["table 'rp_leftover' left behind"])

    async def test_a_column_left_behind_is_reported(self):
        self.conn.execute("CREATE TABLE rp_users (id INTEGER)")
        migrator = self.migrator(
            [
                Migration(
                    "001_c",
                    up="ALTER TABLE rp_users ADD COLUMN bio TEXT",
                    down="SELECT 1",
                )
            ],
        )
        results = await migrator.rehearse()
        self.assertEqual(results[0].reversed, ["column 'rp_users.bio' left behind"])

    async def test_no_down_step_leaves_the_comparison_unchecked(self):
        migrator = self.migrator(
            [Migration("001_t", up="CREATE TABLE rp_t (id INTEGER)")]
        )
        results = await migrator.rehearse()
        self.assertIsNone(results[0].reversed)

    async def test_one_migration_without_a_down_step_spares_the_others(self):
        migrator = self.migrator(
            [
                Migration("001_k", up="CREATE TABLE rp_k (id INTEGER)"),
                Migration(
                    "002_t",
                    up="CREATE TABLE rp_t (id INTEGER)",
                    down="DROP TABLE rp_t",
                ),
            ],
        )
        results = await migrator.rehearse()
        self.assertTrue(results[1].down_ok)
        self.assertIsNone(results[1].reversed)
        self.assertTrue(results.ok)

    async def test_a_repeatable_still_allows_the_reversed_comparison(self):
        migrator = self.migrator(
            [
                Migration(
                    "001_t",
                    up="CREATE TABLE rp_t (id INTEGER)",
                    down="DROP TABLE rp_t",
                ),
                Migration(
                    "vw_rp",
                    up="CREATE VIEW vw_rp AS SELECT 1 AS one",
                    repeatable=True,
                ),
            ],
        )
        results = await migrator.rehearse()
        self.assertTrue(results.ok)
        self.assertEqual(results[0].reversed, [])

    async def test_a_rename_hint_survives_the_landed_check(self):
        self.conn.execute("CREATE TABLE rp_users (id INTEGER, mail VARCHAR(120))")
        migrator = self.migrator([])
        results = await migrator.rehearse(
            models=self.models(), renames={"rp_users.mail": "email"}
        )
        self.assertEqual(results[0].landed, [])
        self.assertTrue(results.ok)

    async def test_a_table_rename_hint_survives_the_landed_check(self):
        self.conn.execute("CREATE TABLE rp_people (id INTEGER, email VARCHAR(120))")
        migrator = self.migrator([])
        results = await migrator.rehearse(
            models=self.models(), table_renames={"rp_people": "rp_users"}
        )
        self.assertEqual(results[0].landed, [])
        self.assertTrue(results.ok)

    async def test_repeatables_rehearse_after_the_generated_migration(self):
        migrator = self.migrator(
            [
                Migration(
                    "vw_rp",
                    up="CREATE VIEW vw_rp AS SELECT id FROM rp_users",
                    repeatable=True,
                )
            ],
        )
        results = await migrator.rehearse(models=self.models())
        self.assertTrue(results[0].id.startswith("auto_"))
        self.assertEqual(results[1].id, "vw_rp")
        self.assertTrue(results.ok)

    async def test_models_rehearse_as_a_migration_of_their_own(self):
        migrator = self.migrator([])
        results = await migrator.rehearse(models=self.models())
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].id.startswith("auto_"))
        self.assertEqual(results[0].landed, [])
        self.assertEqual(results[0].reversed, [])
        self.assertNotIn("rp_users", table_names(self.conn))
        self.assertEqual(await migrator.applied_records(), [])

    async def test_the_generated_migration_runs_after_the_registered_ones(self):
        migrator = self.migrator(
            [
                Migration(
                    "001_t",
                    up="CREATE TABLE rp_t (id INTEGER)",
                    down="DROP TABLE rp_t",
                )
            ],
        )
        results = await migrator.rehearse(models=self.models())
        self.assertEqual(results[0].id, "001_t")
        self.assertTrue(results[1].id.startswith("auto_"))
        self.assertIsNone(results[0].landed)

    async def test_a_change_the_diff_skipped_is_not_reported_as_not_landed(self):
        self.conn.execute("CREATE TABLE rp_users (id INTEGER, email BOOLEAN)")
        migrator = self.migrator([])
        results = await migrator.rehearse(
            models=self.models(bio=String(20)), ignore_changed_columns=True
        )
        self.assertEqual(results[0].landed, [])
        self.assertTrue(results.ok)

    async def test_models_that_match_the_database_rehearse_nothing(self):
        migrator = self.migrator([])
        await migrator.up(models=self.models())
        self.assertEqual(await migrator.rehearse(models=self.models()), [])

    async def test_a_generated_statement_that_fails_stops_the_rehearsal(self):
        # A view the models cannot see: introspection reports tables, so
        # the diff asks for a table the name is already taken by.
        self.conn.execute("CREATE TABLE rp_src (id INTEGER)")
        self.conn.execute("CREATE VIEW rp_users AS SELECT id FROM rp_src")
        migrator = self.migrator([])
        results = await migrator.rehearse(models=self.models(), migration_id="drift")
        self.assertEqual([(r.id, r.up_ok) for r in results], [("drift", False)])
        self.assertIn("rp_users", results[0].error)

    async def test_a_migration_id_names_the_generated_migration(self):
        migrator = self.migrator([])
        results = await migrator.rehearse(models=self.models(), migration_id="drift")
        self.assertEqual(results[0].id, "drift")


class RehearsalRowsCases(BothMigrators):
    """A rehearsal leaves a row the next run can read."""

    def migrations(self):
        return [
            Migration(
                "001_users",
                up="CREATE TABLE rc_users (id INTEGER)",
                down="DROP TABLE rc_users",
            )
        ]

    async def test_a_passing_rehearsal_records_its_key(self):
        migrator = self.migrator(self.migrations())
        rehearsal = await migrator.rehearse()
        self.assertTrue(rehearsal.ok)
        self.assertTrue(rehearsal.recorded)
        self.assertTrue(await migrator.rehearsed(rehearsal.key))
        self.assertEqual(
            await migrator.rehearsal_outcome(rehearsal.key), REHEARSAL_PASSED
        )

    async def test_a_failing_rehearsal_records_the_failure(self):
        broken = Migration("002_bad", up="NOT SQL", down="DROP TABLE nothing")
        migrator = self.migrator(self.migrations() + [broken])
        rehearsal = await migrator.rehearse()
        self.assertFalse(rehearsal.ok)
        self.assertEqual(
            await migrator.rehearsal_outcome(rehearsal.key), REHEARSAL_FAILED
        )
        self.assertFalse(await migrator.rehearsed(rehearsal.key))

    async def test_an_unknown_key_has_no_outcome(self):
        migrator = self.migrator(self.migrations())
        self.assertIsNone(await migrator.rehearsal_outcome("0" * 64))
        self.assertFalse(await migrator.rehearsed("0" * 64))

    async def test_a_second_rehearsal_replaces_the_row(self):
        migrator = self.migrator(self.migrations())
        key = (await migrator.rehearse()).key
        await migrator.record_rehearsal(key, REHEARSAL_FAILED)
        await migrator.record_rehearsal(key, REHEARSAL_PASSED)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM sustained_rehearsals WHERE rehearsal_key = ?",
            (key,),
        ).fetchone()
        self.assertEqual(rows[0], 1)
        self.assertTrue(await migrator.rehearsed(key))

    async def test_an_unknown_outcome_is_refused(self):
        migrator = self.migrator(self.migrations())
        with self.assertRaises(ValueError) as caught:
            await migrator.record_rehearsal("0" * 64, "maybe")
        self.assertIn("passed", str(caught.exception))

    async def test_the_rehearsal_table_is_not_read_as_drift(self):
        migrator = self.migrator([])
        await migrator.up(models=[MigUser])
        await migrator.record_rehearsal("0" * 64)
        self.assertIsNone(await migrator.plan([MigUser], allow_drops=True))


class DestructiveGateCases(BothMigrators):
    """A run that removes data needs a rehearsal that proved it."""

    def setUp(self):
        super().setUp()
        self.conn.execute("CREATE TABLE gate_old (id INTEGER)")
        self.drop = Migration(
            "001_drop",
            up="DROP TABLE gate_old",
            down="CREATE TABLE gate_old (id INTEGER)",
        )

    async def test_an_unrehearsed_drop_is_refused(self):
        migrator = self.migrator([self.drop])
        with self.assertRaises(RehearsalRequired) as caught:
            await migrator.up()
        message = str(caught.exception)
        self.assertIn("no rehearsal has proved these statements", message)
        self.assertIn("001_drop  DROP TABLE gate_old", message)
        self.assertIn("sustained rehearse", message)
        self.assertIn("--unrehearsed", message)
        self.assertEqual(await migrator.applied(), [])

    async def test_a_rehearsed_drop_runs(self):
        migrator = self.migrator([self.drop])
        self.assertTrue((await migrator.rehearse()).ok)
        self.assertEqual(await migrator.up(), ["001_drop"])
        self.assertNotIn("gate_old", table_names(self.conn))

    async def test_the_override_runs_without_a_rehearsal_row(self):
        migrator = self.migrator([self.drop])
        self.assertEqual(await migrator.up(unrehearsed=True), ["001_drop"])

    async def test_the_override_is_recorded_and_unlocks_nothing(self):
        migrator = self.migrator([self.drop])
        await migrator.up(unrehearsed=True)
        key = rehearsal_key((await migrator.applied_records())[:0], [self.drop])
        self.assertEqual(await migrator.rehearsal_outcome(key), "override")
        self.assertFalse(await migrator.rehearsed(key))

    async def test_an_additive_override_records_nothing(self):
        additive = Migration("001_add", up="CREATE TABLE gate_new (id INTEGER)")
        migrator = self.migrator([additive])
        await migrator.up(unrehearsed=True)
        self.assertIsNone(
            await migrator.rehearsal_outcome(rehearsal_key([], [additive])),
        )

    async def test_a_targeted_message_names_the_target(self):
        later = Migration("002_trim", up="DROP TABLE gate_old")
        migrator = self.migrator([self.drop, later])
        with self.assertRaises(RehearsalRequired) as caught:
            await migrator.up(target="001_drop")
        self.assertIn(
            "sustained migrate --target 001_drop --unrehearsed",
            str(caught.exception),
        )

    async def test_a_block_after_the_registered_run_names_what_applied(self):
        registered = Migration(
            "001_add",
            up="CREATE TABLE gate_new (id INTEGER)",
            down="DROP TABLE gate_new",
        )
        migrator = self.migrator([registered])
        with self.assertRaises(RehearsalRequired) as caught:
            await migrator.up(models=[MigUser], allow_drops=True)
        self.assertEqual(getattr(caught.exception, "applied", None), ["001_add"])
        self.assertIn("gate_new", table_names(self.conn))

    async def test_a_failed_rehearsal_reads_differently(self):
        broken = Migration("001_drop", up=["DROP TABLE gate_old", "NOT SQL"])
        migrator = self.migrator([broken])
        self.assertFalse((await migrator.rehearse()).ok)
        with self.assertRaises(RehearsalRequired) as caught:
            await migrator.up()
        self.assertIn(
            "The last rehearsal of these statements failed", str(caught.exception)
        )

    async def test_editing_the_migration_voids_the_row(self):
        await self.migrator([self.drop]).rehearse()
        edited = Migration(
            "001_drop",
            up=["DROP TABLE gate_old", "CREATE TABLE gate_new (id INTEGER)"],
            down="DROP TABLE gate_new",
        )
        with self.assertRaises(RehearsalRequired):
            await self.migrator([edited]).up()

    async def test_an_additive_run_is_never_gated(self):
        additive = Migration("001_add", up="CREATE TABLE gate_new (id INTEGER)")
        self.assertEqual(await self.migrator([additive]).up(), ["001_add"])

    async def test_a_generated_drop_is_gated_and_then_runs(self):
        migrator = self.migrator([])
        with self.assertRaises(RehearsalRequired) as caught:
            await migrator.up(models=[MigUser], allow_drops=True)
        self.assertIn("DROP TABLE", str(caught.exception))
        # The registered migrations that ran before the diff stay applied;
        # here there are none, and the tables the models want are absent.
        self.assertNotIn("mig_users", table_names(self.conn))
        self.assertTrue(
            (await self.migrator([]).rehearse(models=[MigUser], allow_drops=True)).ok
        )
        await self.migrator([]).up(models=[MigUser], allow_drops=True)
        self.assertIn("mig_users", table_names(self.conn))
        self.assertNotIn("gate_old", table_names(self.conn))

    async def test_a_targeted_run_uses_the_prefix_the_rehearsal_proved(self):
        later = Migration(
            "002_add",
            up="CREATE TABLE gate_new (id INTEGER)",
            down="DROP TABLE gate_new",
        )
        migrator = self.migrator([self.drop, later])
        self.assertTrue((await migrator.rehearse()).ok)
        self.assertEqual(await migrator.up(target="001_drop"), ["001_drop"])
        self.assertNotIn("gate_old", table_names(self.conn))
        self.assertNotIn("gate_new", table_names(self.conn))

    async def test_a_targeted_run_past_an_unrehearsed_drop_is_refused(self):
        later = Migration("002_trim", up="DROP TABLE gate_old")
        first = Migration(
            "001_add",
            up="CREATE TABLE gate_new (id INTEGER)",
            down="DROP TABLE gate_new",
        )
        migrator = self.migrator([first, later])
        # The rehearsal covers both. Editing the second voids the prefix
        # that includes it, while the first still applies on its own.
        self.assertTrue((await migrator.rehearse()).ok)
        edited = self.migrator(
            [first, Migration("002_trim", up="DROP TABLE gate_old;")]
        )
        self.assertEqual(await edited.up(target="001_add"), ["001_add"])
        with self.assertRaises(RehearsalRequired):
            await edited.up(target="002_trim")

    async def test_two_targeted_runs_in_a_row_use_one_rehearsal(self):
        second = Migration("002_trim", up="DROP TABLE gate_second")
        self.conn.execute("CREATE TABLE gate_second (id INTEGER)")
        migrator = self.migrator([self.drop, second])
        self.assertTrue((await migrator.rehearse()).ok)
        # The second target starts from a history the first target wrote,
        # and the rehearsal passed through that state on its way up.
        self.assertEqual(await migrator.up(target="001_drop"), ["001_drop"])
        self.assertEqual(await migrator.up(target="002_trim"), ["002_trim"])
        self.assertNotIn("gate_second", table_names(self.conn))

    async def test_an_untargeted_run_after_a_targeted_one_includes_the_repeatables(
        self,
    ):
        first = Migration(
            "001_add",
            up="CREATE TABLE gate_new (id INTEGER)",
            down="DROP TABLE gate_new",
        )
        trim = Migration("002_trim", up="DROP TABLE gate_old")
        seed = Migration("seed", up="SELECT 1", repeatable=True)
        migrator = self.migrator([first, trim, seed])
        self.assertTrue((await migrator.rehearse()).ok)
        self.assertEqual(await migrator.up(target="001_add"), ["001_add"])
        # The rest of the run starts from the history the target wrote and
        # ends with the repeatable, the tail the rehearsal ran.
        self.assertEqual(await migrator.up(), ["002_trim", "seed"])

    async def test_a_row_under_the_legacy_key_opens_the_gate(self):
        later = Migration("002_add", up="CREATE TABLE gate_new (id INTEGER)")
        migrator = self.migrator([self.drop, later])
        # The keys a release before 2.25.0 recorded for the full run and
        # for the targeted prefix.
        for run in ([self.drop, later], [self.drop]):
            await migrator.record_rehearsal(_legacy_rehearsal_key([], run))
        self.assertIsNone(
            await migrator.rehearsal_outcome(rehearsal_key([], [self.drop]))
        )
        self.assertEqual(await migrator.run_outcome([], [self.drop]), REHEARSAL_PASSED)
        self.assertEqual(await migrator.up(target="001_drop"), ["001_drop"])
        self.assertNotIn("gate_old", table_names(self.conn))

    async def test_a_row_under_the_current_key_wins_over_the_legacy_one(self):
        migrator = self.migrator([self.drop])
        await migrator.record_rehearsal(_legacy_rehearsal_key([], [self.drop]))
        await migrator.record_rehearsal(
            rehearsal_key([], [self.drop]), REHEARSAL_FAILED
        )
        self.assertEqual(await migrator.run_outcome([], [self.drop]), REHEARSAL_FAILED)
        with self.assertRaises(RehearsalRequired):
            await migrator.up()

    async def test_a_run_whose_checksums_did_not_change_has_no_legacy_key(self):
        def step(connection):
            return None

        pinned = Migration("001_call", up=step, checksum="abc")
        self.assertIsNone(_legacy_rehearsal_key([], [pinned]))
        self.assertIsNone(await self.migrator([pinned]).run_outcome([], [pinned]))

    async def test_a_rehearsal_writes_every_row_in_one_commit(self):
        migrations = [
            Migration(
                f"00{i}_churn",
                up=[f"CREATE TABLE churn{i} (id INTEGER)", f"DROP TABLE churn{i}"],
                down="SELECT 1",
            )
            for i in range(1, 4)
        ]
        statements = []
        self.conn.set_trace_callback(statements.append)
        self.assertTrue((await self.migrator(migrations).rehearse()).ok)
        written = statements[statements.index("ROLLBACK") :]
        # Every start point and every end point after it removes data, so
        # three migrations prove six keys, and they commit together.
        self.assertEqual(written.count("COMMIT"), 1)
        rows = self.conn.execute(
            "SELECT outcome, rehearsed_at FROM sustained_rehearsals"
        ).fetchall()
        self.assertEqual(len(rows), 6)
        self.assertEqual(len(set(rows)), 1)

    async def test_a_second_rehearsal_replaces_its_rows(self):
        migrator = self.migrator([self.drop])
        self.assertTrue((await migrator.rehearse()).ok)
        self.assertTrue((await migrator.rehearse()).ok)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM sustained_rehearsals"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    async def scratch_rehearsal(self, migrations):
        """A passing rehearsal of the migrations on an empty scratch database."""
        scratch = self.connect()
        scratch.execute("CREATE TABLE gate_old (id INTEGER)")
        return await self.migrator(migrations, connection=scratch).rehearse(
            scratch=True
        )

    async def test_a_scratch_rehearsal_records_on_the_real_database(self):
        later = Migration("002_add", up="CREATE TABLE gate_new (id INTEGER)")
        migrations = [self.drop, later]
        rehearsal = await self.scratch_rehearsal(migrations)
        self.assertTrue(rehearsal.ok)
        self.assertFalse(rehearsal.recorded)
        migrator = self.migrator(migrations)
        key = await migrator.record_scratch_rehearsal(rehearsal)
        self.assertEqual(key, rehearsal_key([], migrations))
        self.assertTrue(await migrator.rehearsed(key))
        # The prefix rows go in too, so a targeted run reads its own row.
        self.assertEqual(await migrator.up(target="001_drop"), ["001_drop"])
        self.assertEqual(await migrator.up(), ["002_add"])

    async def test_a_failed_scratch_rehearsal_records_nothing(self):
        broken = Migration("001_drop", up=["DROP TABLE gate_old", "NOT SQL"])
        rehearsal = await self.scratch_rehearsal([broken])
        self.assertFalse(rehearsal.ok)
        migrator = self.migrator([broken])
        self.assertIsNone(await migrator.record_scratch_rehearsal(rehearsal))
        with self.assertRaises(RehearsalRequired):
            await migrator.up()

    async def test_a_scratch_rehearsal_that_misses_a_pending_migration_records_nothing(
        self,
    ):
        rehearsal = await self.scratch_rehearsal([])
        migrator = self.migrator([self.drop])
        self.assertIsNone(await migrator.record_scratch_rehearsal(rehearsal))
        self.assertIsNone(
            await migrator.rehearsal_outcome(rehearsal_key([], [self.drop]))
        )

    async def test_a_scratch_rehearsal_with_nothing_pending_records_nothing(self):
        migrator = self.migrator([])
        self.assertIsNone(
            await migrator.record_scratch_rehearsal(
                await self.scratch_rehearsal([self.drop])
            )
        )

    async def test_a_rehearsal_with_models_also_covers_the_registered_set(self):
        migrator = self.migrator([self.drop])
        rehearsal = await migrator.rehearse(models=[MigUser])
        self.assertTrue(rehearsal.ok)
        # The rehearsal ran the drop and the generated migration; a run
        # without models applies the drop alone, which it also proved.
        self.assertEqual(await self.migrator([self.drop]).up(), ["001_drop"])


class ScriptCases(BothMigrators):
    """script() renders the same text the sync migrator renders."""

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

    async def test_script_rejects_an_unknown_direction(self):
        migrator = self.migrator(self.migrations())
        with self.assertRaises(ValueError):
            await migrator.script("sideways")


class PlanAndDriftCases(BothMigrators):
    """plan() and drift() report what the sync migrator reports."""

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

    async def test_plan_writes_nothing(self):
        migrator = self.migrator([])
        await migrator.plan(self.models())
        self.assertEqual(table_names(self.conn), set())
        await migrator.drift(self.models())
        self.assertEqual(table_names(self.conn), set())

    async def test_plan_generates_its_own_id(self):
        migrator = self.migrator([])
        generated = await migrator.plan(self.models())
        self.assertTrue(generated.id.startswith("auto_"))

    async def test_plan_returns_none_when_the_schema_matches(self):
        migrator = self.migrator([])
        self.conn.execute(
            "CREATE TABLE async_plan_users (id INTEGER PRIMARY KEY, name TEXT)"
        )
        self.assertIsNone(await migrator.plan(self.models()))
        self.assertEqual(await migrator.drift(self.models()), [])

    async def test_drift_ignores_changed_columns_on_request(self):
        migrator = self.migrator([])
        self.conn.execute(
            "CREATE TABLE async_plan_users (id INTEGER PRIMARY KEY, name INTEGER)"
        )
        self.assertTrue(await migrator.drift(self.models()))
        self.assertEqual(
            await migrator.drift(self.models(), ignore_changed_columns=True), []
        )

    async def test_the_tracking_table_is_left_out_of_the_diff(self):
        migrator = self.migrator([])
        await migrator.up()
        self.conn.execute(
            "CREATE TABLE async_plan_users (id INTEGER PRIMARY KEY, name TEXT)"
        )
        self.assertEqual(await migrator.drift(self.models()), [])
        self.assertIsNone(await migrator.plan(self.models(), allow_drops=True))


class ModelRunsCases(BothMigrators):
    """up(models=...) and rehearse(models=...) on the async migrator."""

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
        migrator = self.migrator(self.migrations())
        applied = await migrator.up(models=self.models())
        self.assertEqual(applied[0], "a")
        self.assertTrue(applied[1].startswith("auto_"))
        self.assertIn("async_run_users", table_names(self.conn))
        self.assertEqual(await migrator.drift(self.models()), [])

    async def test_the_generated_statements_live_on_the_tracking_row(self):
        migrator = self.migrator([])
        await migrator.up(models=self.models(), migration_id="auto_run")
        rows = self.rows()
        self.assertEqual(rows[0][0], "auto_run")
        self.assertEqual(rows[0][1], 1)
        stored = json.loads(rows[0][2])
        self.assertEqual(
            stored["up"],
            ['CREATE TABLE "async_run_users" ("id" INTEGER PRIMARY KEY, "name" TEXT)'],
        )
        self.assertEqual(stored["down"], ['DROP TABLE IF EXISTS "async_run_users"'])

    async def test_a_second_run_generates_nothing(self):
        migrator = self.migrator([])
        await migrator.up(models=self.models())
        self.assertEqual(await migrator.up(models=self.models()), [])

    async def test_models_and_a_target_are_refused(self):
        migrator = self.migrator(self.migrations())
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

        migrator = self.migrator(self.migrations(), guards=[no_new_tables])
        with self.assertRaises(GuardBlocked) as caught:
            await migrator.up(models=self.models())
        self.assertEqual(caught.exception.applied, ["a"])
        self.assertNotIn("async_run_users", table_names(self.conn))
        self.assertEqual([m.id for m in migrator._migrations], ["a"])


class TestRuns(OnMigrator, RunsCases, unittest.IsolatedAsyncioTestCase):
    pass


class TestAsyncRuns(OnAsyncMigrator, RunsCases, unittest.IsolatedAsyncioTestCase):
    pass


class TestTrackingTable(
    OnMigrator, TrackingTableCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestAsyncTrackingTable(
    OnAsyncMigrator, TrackingTableCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestValidateAndRepair(
    OnMigrator, ValidateAndRepairCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestAsyncValidateAndRepair(
    OnAsyncMigrator, ValidateAndRepairCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestNonTransactionalMigrations(
    OnMigrator, NonTransactionalMigrationsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestAsyncNonTransactionalMigrations(
    OnAsyncMigrator, NonTransactionalMigrationsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestBaseline(OnMigrator, BaselineCases, unittest.IsolatedAsyncioTestCase):
    pass


class TestAsyncBaseline(
    OnAsyncMigrator, BaselineCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestPlan(OnMigrator, PlanCases, unittest.IsolatedAsyncioTestCase):
    pass


class TestAsyncPlan(OnAsyncMigrator, PlanCases, unittest.IsolatedAsyncioTestCase):
    pass


class TestRepeatableMigrations(
    OnMigrator, RepeatableMigrationsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestAsyncRepeatableMigrations(
    OnAsyncMigrator, RepeatableMigrationsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestReadOnlyPaths(
    OnMigrator, ReadOnlyPathsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestAsyncReadOnlyPaths(
    OnAsyncMigrator, ReadOnlyPathsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestRehearse(OnMigrator, RehearseCases, unittest.IsolatedAsyncioTestCase):
    pass


class TestAsyncRehearse(
    OnAsyncMigrator, RehearseCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestGeneratedRows(
    OnMigrator, GeneratedRowsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestAsyncGeneratedRows(
    OnAsyncMigrator, GeneratedRowsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestRehearsalProofs(
    OnMigrator, RehearsalProofsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestAsyncRehearsalProofs(
    OnAsyncMigrator, RehearsalProofsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestRehearsalRows(
    OnMigrator, RehearsalRowsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestAsyncRehearsalRows(
    OnAsyncMigrator, RehearsalRowsCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestDestructiveGate(
    OnMigrator, DestructiveGateCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestAsyncDestructiveGate(
    OnAsyncMigrator, DestructiveGateCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestScript(OnMigrator, ScriptCases, unittest.IsolatedAsyncioTestCase):
    pass


class TestAsyncScript(OnAsyncMigrator, ScriptCases, unittest.IsolatedAsyncioTestCase):
    pass


class TestPlanAndDrift(OnMigrator, PlanAndDriftCases, unittest.IsolatedAsyncioTestCase):
    pass


class TestAsyncPlanAndDrift(
    OnAsyncMigrator, PlanAndDriftCases, unittest.IsolatedAsyncioTestCase
):
    pass


class TestModelRuns(OnMigrator, ModelRunsCases, unittest.IsolatedAsyncioTestCase):
    pass


class TestAsyncModelRuns(
    OnAsyncMigrator, ModelRunsCases, unittest.IsolatedAsyncioTestCase
):
    pass
