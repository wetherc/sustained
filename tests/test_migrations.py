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


if __name__ == "__main__":
    unittest.main()
