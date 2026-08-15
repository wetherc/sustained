"""
Tests for the migration runner against in-memory SQLite.
"""

import sqlite3
import unittest
from unittest import mock

from sustained import Model
from sustained.exceptions import MigrationError
from sustained.migrations import (
    Migration,
    Migrator,
    create_table_migration,
    migration_checksum,
)
from sustained.schema import Integer, String


class MigUser(Model):
    tableName = "mig_users"
    tableColumns = {
        "id": Integer(primary_key=True),
        "email": String(120, nullable=False),
    }


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r[0] for r in rows}


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()


class TestMigrator(MigrationTestCase):
    def migrations(self):
        return [
            create_table_migration(MigUser),
            Migration(
                "add_flag",
                up="ALTER TABLE mig_users ADD COLUMN flag INTEGER DEFAULT 0",
                down=["ALTER TABLE mig_users DROP COLUMN flag"],
            ),
        ]

    def test_up_applies_in_order_and_records(self):
        migrator = Migrator(self.conn, self.migrations())
        applied = migrator.up()
        self.assertEqual(applied, ["create_mig_users", "add_flag"])
        self.assertIn("mig_users", table_names(self.conn))
        self.assertEqual(migrator.pending(), [])
        self.assertEqual(
            migrator.status(),
            [("create_mig_users", True), ("add_flag", True)],
        )

    def test_up_is_idempotent(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        self.assertEqual(migrator.up(), [])

    def test_up_to_target(self):
        migrator = Migrator(self.conn, self.migrations())
        applied = migrator.up(target="create_mig_users")
        self.assertEqual(applied, ["create_mig_users"])
        self.assertEqual(len(migrator.pending()), 1)

    def test_unknown_target_raises(self):
        migrator = Migrator(self.conn, self.migrations())
        with self.assertRaises(ValueError):
            migrator.up(target="nope")

    def test_down_reverts_newest_first(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        reverted = migrator.down()
        self.assertEqual(reverted, ["add_flag"])
        self.assertEqual(len(migrator.pending()), 1)
        reverted = migrator.down()
        self.assertEqual(reverted, ["create_mig_users"])
        self.assertNotIn("mig_users", table_names(self.conn))

    def test_down_requires_down_step(self):
        migrator = Migrator(
            self.conn, [Migration("one_way", up="CREATE TABLE ow (id INTEGER)")]
        )
        migrator.up()
        with self.assertRaises(ValueError):
            migrator.down()

    def test_down_requires_registered_migration(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        stripped = Migrator(self.conn, [])
        with self.assertRaises(ValueError):
            stripped.down()

    def test_failed_migration_rolls_back_tracking(self):
        migrations = [
            Migration(
                "ok", up="CREATE TABLE ok_tbl (id INTEGER)", down="DROP TABLE ok_tbl"
            ),
            Migration("boom", up="THIS IS NOT SQL"),
        ]
        migrator = Migrator(self.conn, migrations)
        with self.assertRaises(sqlite3.OperationalError):
            migrator.up()
        self.assertEqual(migrator.applied(), ["ok"])

    def test_callable_step(self):
        seen = []

        def make_it(conn):
            conn.execute("CREATE TABLE cb_tbl (id INTEGER)")
            seen.append(True)

        migrator = Migrator(self.conn, [Migration("cb", up=make_it)])
        migrator.up()
        self.assertTrue(seen)
        self.assertIn("cb_tbl", table_names(self.conn))

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            Migrator(
                self.conn,
                [Migration("a", up="SELECT 1"), Migration("a", up="SELECT 1")],
            )


class TestChecksums(unittest.TestCase):
    def test_string_and_list_steps_hash_the_same_sql(self):
        one = Migration("m", up="  CREATE TABLE t (id INTEGER)  ")
        two = Migration("m", up=["CREATE TABLE t (id INTEGER)"])
        self.assertEqual(migration_checksum(one), migration_checksum(two))

    def test_changed_sql_changes_the_checksum(self):
        one = Migration("m", up="CREATE TABLE t (id INTEGER)")
        two = Migration("m", up="CREATE TABLE t (id BIGINT)")
        self.assertNotEqual(migration_checksum(one), migration_checksum(two))

    def test_callable_step_has_no_checksum(self):
        self.assertIsNone(migration_checksum(Migration("m", up=lambda c: None)))

    def test_explicit_checksum_wins(self):
        migration = Migration("m", up=lambda c: None, checksum="abc123")
        self.assertEqual(migration_checksum(migration), "abc123")


class TestTrackingTable(MigrationTestCase):
    def columns(self):
        rows = self.conn.execute("PRAGMA table_info(sustained_migrations)").fetchall()
        return {r[1] for r in rows}

    def rows(self):
        return self.conn.execute(
            "SELECT id, seq, checksum, applied_at, execution_ms, success "
            "FROM sustained_migrations ORDER BY seq"
        ).fetchall()

    def test_fresh_table_has_full_shape(self):
        Migrator(self.conn, []).up()
        self.assertEqual(
            self.columns(),
            {"id", "seq", "checksum", "applied_at", "execution_ms", "success"},
        )

    def test_apply_records_checksum_seq_timing_and_success(self):
        migration = Migration("one", up="CREATE TABLE t1 (id INTEGER)")
        Migrator(self.conn, [migration]).up()
        (row,) = self.rows()
        self.assertEqual(row[0], "one")
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], migration_checksum(migration))
        self.assertIsInstance(row[4], int)
        self.assertGreaterEqual(row[4], 0)
        self.assertTrue(row[5])

    def test_seq_increments_across_runs(self):
        first = Migrator(self.conn, [Migration("a", up="CREATE TABLE a1 (x INTEGER)")])
        first.up()
        second = Migrator(
            self.conn,
            [
                Migration("a", up="CREATE TABLE a1 (x INTEGER)"),
                Migration("b", up="CREATE TABLE b1 (x INTEGER)"),
            ],
        )
        second.up()
        self.assertEqual([(r[0], r[1]) for r in self.rows()], [("a", 1), ("b", 2)])

    def test_callable_step_records_null_checksum(self):
        Migrator(
            self.conn,
            [Migration("cb", up=lambda c: c.execute("CREATE TABLE cbt (x INTEGER)"))],
        ).up()
        (row,) = self.rows()
        self.assertIsNone(row[2])

    def test_legacy_tracking_table_is_upgraded_in_place(self):
        self.conn.execute(
            "CREATE TABLE sustained_migrations "
            "(id VARCHAR(255) PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "INSERT INTO sustained_migrations VALUES "
            "('old_one', '2024-01-01T00:00:00'), ('old_two', '2024-02-01T00:00:00')"
        )
        self.conn.commit()
        migrator = Migrator(
            self.conn,
            [Migration("old_one", up="SELECT 1"), Migration("old_two", up="SELECT 1")],
        )
        self.assertEqual(migrator.applied(), ["old_one", "old_two"])
        self.assertIn("seq", self.columns())
        self.assertEqual(
            [(r[0], r[1], r[5]) for r in self.rows()],
            [("old_one", 1, 1), ("old_two", 2, 1)],
        )
        self.assertEqual(migrator.up(), [])

    def test_partial_upgrade_keeps_existing_success_values(self):
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
        migrator = Migrator(self.conn, [Migration("good", up="SELECT 1")])
        self.assertEqual(migrator.applied(), ["good"])
        stored = dict(
            self.conn.execute("SELECT id, success FROM sustained_migrations").fetchall()
        )
        self.assertEqual(stored, {"good": 1, "bad": 0})

    def test_script_up_renders_full_bookkeeping_row(self):
        migration = Migration("one", up="CREATE TABLE t1 (id INTEGER)")
        script = Migrator(self.conn, [migration]).script("up")
        self.assertIn("(id, seq, checksum, applied_at, execution_ms, success)", script)
        self.assertIn(f"'{migration_checksum(migration)}'", script)
        self.assertIn("1, ", script)
        self.assertIn("TRUE", script)


class TestValidateAndRepair(MigrationTestCase):
    def test_validate_passes_on_a_clean_history(self):
        migrator = Migrator(
            self.conn, [Migration("a", up="CREATE TABLE va (x INTEGER)")]
        )
        migrator.up()
        self.assertEqual(migrator.validate(), [])

    def test_validate_detects_an_edited_migration(self):
        Migrator(self.conn, [Migration("a", up="CREATE TABLE va (x INTEGER)")]).up()
        edited = Migrator(self.conn, [Migration("a", up="CREATE TABLE va (x BIGINT)")])
        with self.assertRaises(MigrationError):
            edited.validate()
        problems = edited.validate(raise_on_problems=False)
        self.assertEqual(len(problems), 1)
        self.assertIn("checksum mismatch", problems[0])

    def test_validate_detects_an_unregistered_applied_migration(self):
        Migrator(self.conn, [Migration("a", up="CREATE TABLE va (x INTEGER)")]).up()
        problems = Migrator(self.conn, []).validate(raise_on_problems=False)
        self.assertIn("not registered", problems[0])

    def test_up_refuses_an_edited_migration_unless_told_not_to_validate(self):
        Migrator(self.conn, [Migration("a", up="CREATE TABLE va (x INTEGER)")]).up()
        edited = Migrator(
            self.conn,
            [
                Migration("a", up="CREATE TABLE va (x BIGINT)"),
                Migration("b", up="CREATE TABLE vb (x INTEGER)"),
            ],
        )
        with self.assertRaises(MigrationError):
            edited.up()
        self.assertEqual(edited.up(validate=False), ["b"])

    def test_out_of_order_pending_migration_is_refused_by_default(self):
        Migrator(
            self.conn,
            [
                Migration("a", up="CREATE TABLE oa (x INTEGER)"),
                Migration("c", up="CREATE TABLE oc (x INTEGER)"),
            ],
        ).up()
        late = Migrator(
            self.conn,
            [
                Migration("a", up="CREATE TABLE oa (x INTEGER)"),
                Migration("b", up="CREATE TABLE ob (x INTEGER)"),
                Migration("c", up="CREATE TABLE oc (x INTEGER)"),
            ],
        )
        with self.assertRaises(MigrationError):
            late.up()
        self.assertEqual(late.up(allow_out_of_order=True), ["b"])
        self.assertEqual(late.validate(), [])

    def test_repair_accepts_an_edited_migration(self):
        Migrator(self.conn, [Migration("a", up="CREATE TABLE va (x INTEGER)")]).up()
        edited = Migrator(self.conn, [Migration("a", up="CREATE TABLE va (x BIGINT)")])
        actions = edited.repair()
        self.assertEqual(actions, ["updated the stored checksum of 'a'"])
        self.assertEqual(edited.validate(), [])

    def test_repair_leaves_a_changed_repeatable_pending(self):
        Migrator(
            self.conn,
            [Migration("r", up="CREATE VIEW rv AS SELECT 1", repeatable=True)],
        ).up()
        changed = Migrator(
            self.conn,
            [Migration("r", up="CREATE VIEW rv2 AS SELECT 2", repeatable=True)],
        )
        self.assertEqual(changed.repair(), [])
        self.assertEqual([m.id for m in changed.pending()], ["r"])
        self.assertEqual(changed.up(), ["r"])

    def test_repair_still_removes_a_repeatable_failure_row(self):
        migration = Migration("r", up="CREATE VIEW rv AS SELECT 1", repeatable=True)
        migrator = Migrator(self.conn, [migration])
        migrator.applied_records()
        with mock.patch.object(
            migrator._compiler, "supports_transactions", return_value=False
        ):
            migrator._record_failure(migration, 1)
        self.assertEqual(migrator.repair(), ["removed the failed attempt of 'r'"])
        self.assertEqual(migrator.validate(), [])

    def test_tag_migration_never_masks_the_original_error(self):
        from sustained.migrations import _tag_migration

        class Frozen(Exception):
            def __setattr__(self, name, value):
                raise AttributeError(name)

        error = Frozen("boom")
        _tag_migration(error, "m1")
        self.assertFalse(hasattr(error, "migration_id"))

    def test_repair_adopts_legacy_rows_without_checksums(self):
        self.conn.execute(
            "CREATE TABLE sustained_migrations "
            "(id VARCHAR(255) PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "INSERT INTO sustained_migrations VALUES ('a', '2024-01-01T00:00:00')"
        )
        self.conn.commit()
        migration = Migration("a", up="CREATE TABLE va (x INTEGER)")
        migrator = Migrator(self.conn, [migration])
        self.assertEqual(migrator.repair(), ["updated the stored checksum of 'a'"])
        stored = self.conn.execute(
            "SELECT checksum FROM sustained_migrations WHERE id = 'a'"
        ).fetchone()[0]
        self.assertEqual(stored, migration_checksum(migration))


class TestFailureTracking(MigrationTestCase):
    def _bare_migrator(self, migrations):
        """A migrator whose engine reports no transaction support."""
        migrator = Migrator(self.conn, migrations)
        patcher = mock.patch.object(
            migrator._compiler, "supports_transactions", return_value=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return migrator

    def test_failed_step_records_a_failure_row_without_transactions(self):
        migrator = self._bare_migrator([Migration("bad", up="THIS IS NOT SQL")])
        with self.assertRaises(sqlite3.OperationalError):
            migrator.up()
        row = self.conn.execute(
            "SELECT id, success FROM sustained_migrations"
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("bad", 0))
        self.assertEqual(migrator.applied(), [])

    def test_failure_row_blocks_up_until_repair(self):
        migrator = self._bare_migrator(
            [Migration("bad", up="CREATE TABLE ft (x INTEGER)")]
        )
        migrator.applied_records()
        migrator._record_failure(migrator._migrations[0], 1)
        with self.assertRaises(MigrationError):
            migrator.up()
        actions = migrator.repair()
        self.assertEqual(actions, ["removed the failed attempt of 'bad'"])
        self.assertEqual(migrator.up(), ["bad"])
        self.assertEqual(migrator.applied(), ["bad"])

    def test_transactional_failure_leaves_no_row(self):
        migrator = Migrator(self.conn, [Migration("bad", up="THIS IS NOT SQL")])
        with self.assertRaises(sqlite3.OperationalError):
            migrator.up()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM sustained_migrations"
        ).fetchone()[0]
        self.assertEqual(count, 0)


class TestBaseline(MigrationTestCase):
    def migrations(self):
        return [
            create_table_migration(MigUser),
            Migration(
                "add_flag",
                up="ALTER TABLE mig_users ADD COLUMN flag INTEGER DEFAULT 0",
                down="ALTER TABLE mig_users DROP COLUMN flag",
            ),
        ]

    def test_baseline_records_without_running(self):
        migrations = self.migrations()
        migrator = Migrator(self.conn, migrations)
        recorded = migrator.baseline("create_mig_users")
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

    def test_baseline_then_up_applies_only_the_rest(self):
        self.conn.executescript(
            "CREATE TABLE mig_users (id INTEGER PRIMARY KEY, "
            "email VARCHAR(120) NOT NULL)"
        )
        migrator = Migrator(self.conn, self.migrations())
        migrator.baseline("create_mig_users")
        self.assertEqual(migrator.validate(), [])
        self.assertEqual(migrator.up(), ["add_flag"])
        self.assertEqual(migrator.applied(), ["create_mig_users", "add_flag"])

    def test_baseline_skips_already_applied(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up(target="create_mig_users")
        self.assertEqual(migrator.baseline("add_flag"), ["add_flag"])
        seqs = [r.seq for r in migrator.applied_records()]
        self.assertEqual(seqs, [1, 2])

    def test_baseline_unknown_target_raises(self):
        migrator = Migrator(self.conn, self.migrations())
        with self.assertRaises(ValueError):
            migrator.baseline("nope")


class TestPlan(MigrationTestCase):
    def test_plan_returns_migration_without_touching_anything(self):
        migrator = Migrator(self.conn, [])
        migration = migrator.plan([MigUser], migration_id="planned")
        self.assertEqual(migration.id, "planned")
        self.assertTrue(any("CREATE TABLE" in s for s in migration.up))
        self.assertNotIn("mig_users", table_names(self.conn))
        self.assertEqual(migrator.status(), [])

    def test_plan_returns_none_when_schema_is_current(self):
        migrator = Migrator(self.conn, [])
        migrator.sync([MigUser])
        self.assertIsNone(migrator.plan([MigUser]))


class TestRepeatableMigrations(MigrationTestCase):
    def migrations(self, view_sql="SELECT id FROM t"):
        return [
            Migration("001_t", up="CREATE TABLE t (id INTEGER)", down="DROP TABLE t"),
            Migration(
                "active_view",
                up=f"CREATE VIEW IF NOT EXISTS v AS {view_sql}",
                repeatable=True,
            ),
        ]

    def test_repeatable_rejects_down_step(self):
        with self.assertRaisesRegex(ValueError, "down step"):
            Migration("r", up="SELECT 1", down="SELECT 2", repeatable=True)

    def test_repeatable_callable_requires_checksum(self):
        with self.assertRaisesRegex(ValueError, "checksum"):
            Migration("r", up=lambda conn: None, repeatable=True)
        Migration("r", up=lambda conn: None, checksum="abc", repeatable=True)

    def test_runs_after_versioned_and_records_once(self):
        migrator = Migrator(self.conn, self.migrations())
        self.assertEqual(migrator.up(), ["001_t", "active_view"])
        self.assertEqual(migrator.up(), [])
        records = {r.id: r for r in migrator.applied_records()}
        self.assertEqual(records["active_view"].seq, 2)

    def test_changed_checksum_reruns_and_updates_in_place(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        first_seq = {r.id: r.seq for r in migrator.applied_records()}
        self.conn.execute("DROP VIEW v")
        changed = Migrator(self.conn, self.migrations("SELECT id, id AS b FROM t"))
        self.assertEqual(changed.up(), ["active_view"])
        records = {r.id: r for r in changed.applied_records()}
        self.assertEqual(records["active_view"].seq, first_seq["active_view"])
        self.assertEqual(len(changed.applied_records()), 2)

    def test_changed_checksum_is_not_a_validation_problem(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        changed = Migrator(self.conn, self.migrations("SELECT id, id AS b FROM t"))
        self.assertEqual(changed.validate(), [])

    def test_statuses_reports_changed(self):
        migrator = Migrator(self.conn, self.migrations())
        self.assertEqual(
            migrator.statuses(),
            [("001_t", "pending"), ("active_view", "pending")],
        )
        migrator.up()
        self.assertEqual(
            migrator.statuses(),
            [("001_t", "applied"), ("active_view", "applied")],
        )
        changed = Migrator(self.conn, self.migrations("SELECT id, id AS b FROM t"))
        self.assertEqual(
            changed.statuses(),
            [("001_t", "applied"), ("active_view", "changed")],
        )

    def test_pending_includes_changed_repeatable(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        changed = Migrator(self.conn, self.migrations("SELECT id, id AS b FROM t"))
        self.assertEqual([m.id for m in changed.pending()], ["active_view"])

    def test_down_skips_repeatables(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        self.assertEqual(migrator.down(), ["001_t"])
        applied = {r.id for r in migrator.applied_records() if r.success}
        self.assertEqual(applied, {"active_view"})

    def test_down_to_skips_repeatables(self):
        migrations = self.migrations()
        migrations.insert(
            1,
            Migration("002_u", up="CREATE TABLE u (id INTEGER)", down="DROP TABLE u"),
        )
        migrator = Migrator(self.conn, migrations)
        migrator.up()
        self.assertEqual(migrator.down_to("001_t"), ["002_u"])

    def test_target_skips_repeatables_and_rejects_repeatable_target(self):
        migrations = self.migrations()
        migrations.insert(
            1,
            Migration("002_u", up="CREATE TABLE u (id INTEGER)", down="DROP TABLE u"),
        )
        migrator = Migrator(self.conn, migrations)
        self.assertEqual(migrator.up(target="001_t"), ["001_t"])
        with self.assertRaisesRegex(ValueError, "repeatable"):
            migrator.up(target="active_view")
        self.assertEqual(migrator.up(), ["002_u", "active_view"])

    def test_baseline_records_repeatables_at_current_checksum(self):
        self.conn.execute("CREATE TABLE t (id INTEGER)")
        self.conn.execute("CREATE VIEW v AS SELECT id FROM t")
        migrator = Migrator(self.conn, self.migrations())
        self.assertEqual(migrator.baseline("001_t"), ["001_t", "active_view"])
        self.assertEqual(migrator.up(), [])
        with self.assertRaisesRegex(ValueError, "repeatable"):
            migrator.baseline("active_view")

    def test_script_up_renders_insert_then_update(self):
        migrator = Migrator(self.conn, self.migrations())
        script = migrator.script("up")
        self.assertIn("-- repeat: active_view", script)
        self.assertIn("INSERT INTO", script)
        migrator.up()
        changed = Migrator(self.conn, self.migrations("SELECT id, id AS b FROM t"))
        script = changed.script("up")
        self.assertIn("-- repeat: active_view", script)
        self.assertIn("UPDATE", script)
        self.assertNotIn("-- up:", script)

    def test_script_down_skips_repeatables(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        script = migrator.script("down")
        self.assertNotIn("active_view", script)
        self.assertIn("001_t", script)

    def test_out_of_order_check_ignores_repeatables(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        migrations = self.migrations()
        migrations.append(
            Migration("002_u", up="CREATE TABLE u (id INTEGER)", down="DROP TABLE u")
        )
        later = Migrator(self.conn, migrations)
        self.assertEqual(later.validate(), [])
        self.assertEqual(later.up(), ["002_u"])

    def test_failed_repeatable_rerun_updates_failure_row(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        broken = Migrator(
            self.conn,
            [
                self.migrations()[0],
                Migration("active_view", up="SELECT * FROM missing", repeatable=True),
            ],
        )
        with mock.patch.object(
            broken._compiler, "supports_transactions", return_value=False
        ):
            with self.assertRaises(sqlite3.OperationalError):
                broken.up(validate=False)
        records = {r.id: r for r in broken.applied_records()}
        self.assertFalse(records["active_view"].success)
        self.assertEqual(len(broken.applied_records()), 2)


class TestRehearse(MigrationTestCase):
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

    def test_rehearse_proves_both_directions_and_changes_nothing(self):
        migrator = Migrator(self.conn, self.migrations())
        results = migrator.rehearse()
        self.assertEqual(
            [(r.id, r.up_ok, r.down_ok) for r in results],
            [
                ("001_users", True, True),
                ("002_flags", True, True),
                ("r_view", True, None),
            ],
        )
        self.assertEqual(results[2].error, "no down step (repeatable)")
        self.assertEqual(table_names(self.conn), {"sustained_migrations"})
        self.assertEqual(migrator.applied_records(), [])
        self.assertEqual(len(migrator.pending()), 3)

    def test_rehearse_after_a_partial_run_covers_only_the_rest(self):
        migrator = Migrator(self.conn, self.migrations())
        # A targeted run skips the repeatables, so 002 and the view remain.
        migrator.up(target="001_users")
        results = migrator.rehearse()
        self.assertEqual(
            [(r.id, r.down_ok) for r in results],
            [("002_flags", True), ("r_view", None)],
        )
        self.assertIn("r_users", table_names(self.conn))
        self.assertNotIn("r_flags", table_names(self.conn))
        self.assertEqual(migrator.applied(), ["001_users"])

    def test_rehearse_reruns_a_changed_repeatable_without_recording_it(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        before = {r.id: r.checksum for r in migrator.applied_records()}
        changed = self.migrations()[:2] + [
            Migration("r_view", up="CREATE VIEW r_v2 AS SELECT 2", repeatable=True)
        ]
        later = Migrator(self.conn, changed)
        results = later.rehearse()
        self.assertEqual([r.id for r in results], ["r_view"])
        self.assertTrue(results[0].up_ok)
        after = {r.id: r.checksum for r in later.applied_records()}
        self.assertEqual(before, after)

    def test_nothing_pending_rehearses_nothing(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        self.assertEqual(migrator.rehearse(), [])

    def test_failing_up_step_stops_the_rehearsal(self):
        migrations = [
            self.migrations()[0],
            Migration("002_bad", up="CRATE TABLE oops", down="DROP TABLE oops"),
            Migration("003_never", up="CREATE TABLE r_never (id INTEGER)"),
        ]
        results = Migrator(self.conn, migrations).rehearse()
        self.assertEqual(
            [(r.id, r.up_ok) for r in results],
            [("001_users", True), ("002_bad", False)],
        )
        self.assertIn("syntax error", results[1].error)
        self.assertEqual(results[0].error, "down not rehearsed: the run stopped")
        self.assertEqual(table_names(self.conn), {"sustained_migrations"})

    def test_missing_down_step_stops_the_down_sweep(self):
        migrations = [
            self.migrations()[0],
            Migration("002_forward", up="CREATE TABLE r_fwd (id INTEGER)"),
        ]
        results = Migrator(self.conn, migrations).rehearse()
        self.assertEqual([r.down_ok for r in results], [None, None])
        self.assertEqual(results[1].error, "no down step")
        self.assertEqual(
            results[0].error, "down not reached: '002_forward' has no down step"
        )

    def test_failing_down_step_is_reported_and_stops_the_sweep(self):
        migrations = [
            self.migrations()[0],
            Migration(
                "002_bad_down",
                up="CREATE TABLE r_bad (id INTEGER)",
                down="DROP TABLE r_missing",
            ),
        ]
        results = Migrator(self.conn, migrations).rehearse()
        self.assertEqual(
            [(r.id, r.down_ok) for r in results][1], ("002_bad_down", False)
        )
        self.assertIn("r_missing", results[1].error)
        self.assertEqual(
            results[0].error, "down not reached: '002_bad_down' down failed"
        )
        self.assertEqual(table_names(self.conn), {"sustained_migrations"})

    def test_validation_problems_stop_the_rehearsal(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        edited = [
            Migration(
                "001_users",
                up="CREATE TABLE r_users (id INTEGER, extra TEXT)",
                down="DROP TABLE r_users",
            )
        ] + self.migrations()[1:]
        with self.assertRaises(MigrationError):
            Migrator(self.conn, edited).rehearse()

    def test_rehearse_refuses_a_dialect_that_cannot_roll_back(self):
        from sustained.dialects import Dialects

        migrator = Migrator(self.conn, self.migrations(), dialect=Dialects.ATHENA)
        with self.assertRaises(ValueError) as caught:
            migrator.rehearse()
        self.assertIn("athena is not on that list", str(caught.exception))
        self.assertIn("get_rehearsal_connection()", str(caught.exception))

    def test_scratch_waives_the_dialect_check(self):
        from sustained.dialects import Dialects

        # The dialect drives the check; the compiler stays SQLite's so the
        # statements still run here.
        migrator = Migrator(self.conn, [self.migrations()[0]])
        migrator._dialect = Dialects.MSSQL
        with self.assertRaises(ValueError):
            migrator.rehearse()
        results = migrator.rehearse(scratch=True)
        self.assertEqual(
            [(r.id, r.up_ok, r.down_ok) for r in results], [("001_users", True, True)]
        )
        self.assertEqual(table_names(self.conn), {"sustained_migrations"})

    def test_rehearse_refuses_an_autocommit_connection(self):
        conn = sqlite3.connect(":memory:", autocommit=True)
        self.addCleanup(conn.close)
        with self.assertRaises(ValueError) as caught:
            Migrator(conn, self.migrations()).rehearse()
        self.assertIn("autocommit", str(caught.exception))

    def test_rehearse_refuses_inside_an_open_transaction(self):
        from sustained.execution import transaction

        migrator = Migrator(self.conn, self.migrations())
        with transaction(self.conn):
            with self.assertRaises(ValueError) as caught:
                migrator.rehearse()
        self.assertIn("open transaction()", str(caught.exception))

    def test_rehearsal_writes_no_failure_row_without_transactions(self):
        migrations = [Migration("002_bad", up="CRATE TABLE oops")]
        migrator = Migrator(self.conn, migrations)
        with mock.patch.object(
            migrator._compiler, "supports_transactions", return_value=False
        ):
            results = migrator.rehearse()
        self.assertFalse(results[0].up_ok)
        self.assertEqual(migrator.applied_records(), [])


if __name__ == "__main__":
    unittest.main()
