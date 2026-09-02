"""
Tests for the migration runner against in-memory SQLite.
"""

import sqlite3
import unittest
from unittest import mock

try:
    import duckdb

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False

import sustained.migrations as migrations_module
from sustained import Model
from sustained.ddl import drop_table
from sustained.dialects import Dialects
from sustained.exceptions import MigrationError, RehearsalRequired
from sustained.migrations import (
    REHEARSAL_FAILED,
    REHEARSAL_PASSED,
    AppliedRecord,
    Migration,
    Migrator,
    _destructive_prefix_keys,
    checked_unique_ids,
    create_table_migration,
    migration_checksum,
    rehearsal_key,
)
from sustained.schema import Integer, String


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

    def test_up_takes_the_diff_options_by_keyword_only(self):
        migrator = Migrator(self.conn, self.migrations())
        with self.assertRaises(TypeError):
            migrator.up(None, True, False, True)

    def test_down_reverts_newest_first(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        reverted = migrator.down()
        self.assertEqual(reverted, ["add_flag"])
        self.assertEqual(len(migrator.pending()), 1)
        reverted = migrator.down()
        self.assertEqual(reverted, ["create_mig_users"])
        self.assertNotIn("mig_users", table_names(self.conn))

    def test_down_refuses_a_negative_step_count(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        for steps in (-1, -2):
            with self.subTest(steps=steps):
                with self.assertRaises(ValueError) as caught:
                    migrator.down(steps=steps)
                self.assertIn("steps must be 0 or more", str(caught.exception))
        self.assertEqual(migrator.applied(), ["create_mig_users", "add_flag"])

    def test_down_with_zero_steps_reverts_nothing(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        self.assertEqual(migrator.down(steps=0), [])
        self.assertEqual(migrator.applied(), ["create_mig_users", "add_flag"])

    def test_down_requires_down_step(self):
        migrator = Migrator(
            self.conn, [Migration("one_way", up="CREATE TABLE ow (id INTEGER)")]
        )
        migrator.up()
        with self.assertRaises(ValueError):
            migrator.down()

    def test_down_refuses_a_migration_edited_after_it_applied(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        edited = self.migrations()
        edited[1] = Migration(
            "add_flag",
            up="ALTER TABLE mig_users ADD COLUMN flag INTEGER DEFAULT 1",
            down=["ALTER TABLE mig_users DROP COLUMN flag"],
        )
        later = Migrator(self.conn, edited)
        with self.assertRaises(MigrationError) as caught:
            later.down()
        message = str(caught.exception)
        self.assertIn("changed after it was applied", message)
        self.assertIn("allow_changed=True", message)
        self.assertEqual(later.applied(), ["create_mig_users", "add_flag"])
        # The flag says the caller knows, so the revert runs.
        self.assertEqual(later.down(allow_changed=True), ["add_flag"])

    def test_down_reverts_nothing_when_an_older_migration_changed(self):
        migrations = self.migrations() + [
            Migration(
                "add_note",
                up="ALTER TABLE mig_users ADD COLUMN note TEXT",
                down=["ALTER TABLE mig_users DROP COLUMN note"],
            )
        ]
        Migrator(self.conn, migrations).up()
        edited = list(migrations)
        edited[1] = Migration(
            "add_flag",
            up="ALTER TABLE mig_users ADD COLUMN flag INTEGER DEFAULT 1",
            down=["ALTER TABLE mig_users DROP COLUMN flag"],
        )
        later = Migrator(self.conn, edited)
        with self.assertRaises(MigrationError):
            later.down(steps=2)
        self.assertEqual(later.applied(), ["create_mig_users", "add_flag", "add_note"])
        self.assertIn("note", column_names(self.conn, "mig_users"))

    def test_down_reverts_nothing_when_an_older_migration_has_no_down_step(self):
        migrations = [
            create_table_migration(MigUser),
            Migration("one_way", up="CREATE TABLE ow (id INTEGER)"),
            Migration(
                "add_note",
                up="ALTER TABLE mig_users ADD COLUMN note TEXT",
                down=["ALTER TABLE mig_users DROP COLUMN note"],
            ),
        ]
        migrator = Migrator(self.conn, migrations)
        migrator.up()
        with self.assertRaises(ValueError):
            migrator.down(steps=2)
        self.assertEqual(
            migrator.applied(), ["create_mig_users", "one_way", "add_note"]
        )
        self.assertIn("note", column_names(self.conn, "mig_users"))

    def test_down_to_carries_the_changed_flag(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        edited = self.migrations()
        edited[1] = Migration(
            "add_flag",
            up="ALTER TABLE mig_users ADD COLUMN flag INTEGER DEFAULT 1",
            down=["ALTER TABLE mig_users DROP COLUMN flag"],
        )
        later = Migrator(self.conn, edited)
        with self.assertRaises(MigrationError):
            later.down_to("create_mig_users")
        self.assertEqual(
            later.down_to("create_mig_users", allow_changed=True), ["add_flag"]
        )

    def test_down_accepts_a_migration_whose_checksum_matches(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        self.assertEqual(migrator.down(), ["add_flag"])

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

    def test_a_checksum_on_a_sql_step_is_refused(self):
        steps = [
            "CREATE TABLE t (id INTEGER)",
            ["CREATE TABLE t (id INTEGER)"],
            [drop_table("t")],
        ]
        for up in steps:
            with self.subTest(up=up):
                with self.assertRaises(ValueError) as caught:
                    Migration("m", up=up, down=None, checksum="abc123")
                self.assertIn("checksum on a step made of SQL", str(caught.exception))


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


class SwitchingConnection:
    """
    A sqlite3 connection with a driver-style autocommit switch, like the
    one psycopg2 carries. It records every switch it is given.
    """

    def __init__(self, connection):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "switches", [])
        object.__setattr__(self, "autocommit", False)

    def __setattr__(self, name, value):
        if name == "autocommit":
            self.switches.append(value)
        object.__setattr__(self, name, value)

    def cursor(self):
        return self._connection.cursor()

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()


class RefusingSwitchConnection(SwitchingConnection):
    """A connection whose driver refuses to leave autocommit again."""

    def __setattr__(self, name, value):
        if name == "autocommit" and value is False and self.switches:
            self.switches.append(value)
            raise RuntimeError("cannot leave autocommit")
        super().__setattr__(name, value)


class TestNonTransactionalMigrations(MigrationTestCase):
    def test_migrations_are_transactional_by_default(self):
        self.assertTrue(Migration("m", up="SELECT 1").transactional)

    def test_flag_is_kept(self):
        migration = Migration("m", up="SELECT 1", transactional=False)
        self.assertFalse(migration.transactional)

    def test_it_applies_and_records_its_row(self):
        migrator = Migrator(
            self.conn,
            [
                Migration(
                    "nt",
                    up="CREATE TABLE nt (x INTEGER)",
                    down="DROP TABLE nt",
                    transactional=False,
                )
            ],
        )
        self.assertEqual(migrator.up(), ["nt"])
        self.assertEqual(migrator.applied(), ["nt"])
        self.assertIn("nt", table_names(self.conn))

    def test_it_reverts(self):
        migrator = Migrator(
            self.conn,
            [
                Migration(
                    "nt",
                    up="CREATE TABLE nt (x INTEGER)",
                    down="DROP TABLE nt",
                    transactional=False,
                )
            ],
        )
        migrator.up()
        self.assertEqual(migrator.down(), ["nt"])
        self.assertNotIn("nt", table_names(self.conn))

    def test_a_failure_leaves_the_earlier_statements_and_a_row(self):
        migrator = Migrator(
            self.conn,
            [
                Migration(
                    "nt",
                    up=["CREATE TABLE nt (x INTEGER)", "THIS IS NOT SQL"],
                    transactional=False,
                )
            ],
        )
        with self.assertRaises(sqlite3.OperationalError):
            migrator.up()
        self.assertIn("nt", table_names(self.conn))
        row = self.conn.execute(
            "SELECT id, success FROM sustained_migrations"
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("nt", 0))
        self.assertEqual(migrator.applied(), [])

    def test_the_driver_switch_goes_off_and_back_on(self):
        connection = SwitchingConnection(self.conn)
        migrator = Migrator(
            connection,
            [Migration("nt", up="CREATE TABLE nt (x INTEGER)", transactional=False)],
        )
        self.assertEqual(migrator.up(), ["nt"])
        self.assertEqual(connection.switches, [True, False])
        self.assertFalse(connection.autocommit)

    def test_the_driver_switch_comes_back_after_a_failure(self):
        connection = SwitchingConnection(self.conn)
        migrator = Migrator(
            connection, [Migration("nt", up="THIS IS NOT SQL", transactional=False)]
        )
        with self.assertRaises(sqlite3.OperationalError):
            migrator.up()
        self.assertFalse(connection.autocommit)

    def test_a_refused_switch_back_keeps_the_migration_error(self):
        connection = RefusingSwitchConnection(self.conn)
        migrator = Migrator(
            connection, [Migration("nt", up="THIS IS NOT SQL", transactional=False)]
        )
        with self.assertRaises(sqlite3.OperationalError) as caught:
            migrator.up()
        self.assertEqual(getattr(caught.exception, "migration_id", None), "nt")
        self.assertEqual(connection.switches, [True, False])

    def test_a_refused_switch_back_after_a_clean_run_is_dropped(self):
        connection = RefusingSwitchConnection(self.conn)
        migrator = Migrator(
            connection,
            [Migration("nt", up="CREATE TABLE nt (x INTEGER)", transactional=False)],
        )
        self.assertEqual(migrator.up(), ["nt"])
        self.assertEqual(connection.switches, [True, False])

    def test_a_rehearsal_still_runs_it_inside_the_rehearsal(self):
        migrator = Migrator(
            self.conn,
            [
                Migration(
                    "nt",
                    up="CREATE TABLE nt (x INTEGER)",
                    down="DROP TABLE nt",
                    transactional=False,
                )
            ],
        )
        rehearsal = migrator.rehearse()
        self.assertTrue(rehearsal.ok)
        self.assertNotIn("nt", table_names(self.conn))


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
        migrator.up(models=[MigUser])
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


class TestReadOnlyPaths(MigrationTestCase):
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

    def test_script_creates_no_tracking_table(self):
        migrator = Migrator(self.conn, self.migrations())
        for direction in ("up", "down"):
            with self.subTest(direction=direction):
                migrator.script(direction)
                self.assertNotIn("sustained_migrations", table_names(self.conn))

    def test_plan_style_reads_create_no_tracking_table(self):
        migrator = Migrator(self.conn, self.migrations())
        self.assertEqual([m.id for m in migrator.pending()], ["001_t"])
        self.assertEqual(migrator.status(), [("001_t", False)])
        self.assertEqual(migrator.statuses(), [("001_t", "pending")])
        self.assertEqual(migrator.validate(), [])
        self.assertEqual(migrator.read_applied_records(), [])
        self.assertEqual(migrator.read_applied(), [])
        self.assertNotIn("sustained_migrations", table_names(self.conn))

    def test_a_read_reports_the_rows_once_the_table_is_there(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        self.assertEqual(migrator.read_applied(), ["001_t"])
        self.assertIn("-- down: 001_t", migrator.script("down"))
        # A fresh migrator has not created the table itself, so the read
        # goes through the probe rather than the ready flag.
        later = Migrator(self.conn, self.migrations())
        self.assertEqual([r.id for r in later.read_applied_records()], ["001_t"])

    def test_a_tracking_table_of_the_old_shape_reads_as_empty(self):
        self.conn.execute("CREATE TABLE sustained_migrations (id TEXT)")
        self.conn.execute("INSERT INTO sustained_migrations VALUES ('001_t')")
        migrator = Migrator(self.conn, self.migrations())
        self.assertEqual(migrator.read_applied_records(), [])

    def test_up_still_creates_the_tracking_table(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        self.assertIn("sustained_migrations", table_names(self.conn))
        self.assertEqual([r.id for r in migrator.applied_records()], ["001_t"])


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
        self.assertEqual(table_names(self.conn), SUSTAINED_TABLES)
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
        self.assertEqual(table_names(self.conn), SUSTAINED_TABLES)

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
        self.assertEqual(table_names(self.conn), SUSTAINED_TABLES)

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
        # A scratch rehearsal writes no row: it belongs on the
        # database the next run will read, not on the throwaway one.
        self.assertEqual(table_names(self.conn), {"sustained_migrations"})
        self.assertFalse(results.recorded)

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


class TestGeneratedRows(MigrationTestCase):
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

    def test_a_generated_row_is_marked_and_validates_elsewhere(self):
        Migrator(self.conn, []).up(models=self.models())
        record = Migrator(self.conn, []).applied_records()[0]
        self.assertTrue(record.generated)
        self.assertEqual(Migrator(self.conn, []).validate(), [])

    def test_a_failed_generated_migration_is_not_kept(self):
        migrator = Migrator(self.conn, [])
        models = self.models()
        real_run_step = migrations_module._run_step

        def refuse(connection, step, compiler):
            raise RuntimeError("no")

        with mock.patch.object(migrations_module, "_run_step", refuse):
            with self.assertRaises(RuntimeError):
                migrator.up(models=models)
        # The failed migration is gone from the list, so the next run
        # diffs the models again instead of repeating the failed SQL.
        self.assertEqual(migrator._migrations, [])
        applied = migrator.up(models=models)
        self.assertEqual(len(applied), 1)
        self.assertIn("gen_users", table_names(self.conn))
        self.assertIs(migrations_module._run_step, real_run_step)

    def test_a_later_process_can_revert_a_generated_migration(self):
        Migrator(self.conn, []).up(models=self.models())
        self.assertIn("gen_users", table_names(self.conn))
        # A migrator that never saw the diff: the statements come off the
        # tracking row.
        later = Migrator(self.conn, [])
        applied_id = later.applied()[0]
        self.assertEqual(later.down(), [applied_id])
        self.assertNotIn("gen_users", table_names(self.conn))
        self.assertEqual(later.applied(), [])

    def test_a_generated_migration_without_a_down_step_still_refuses(self):
        Migrator(self.conn, []).up(models=self.models())
        applied_id = Migrator(self.conn, []).applied()[0]
        self.conn.execute(
            "UPDATE sustained_migrations SET steps = ? WHERE id = ?",
            ('{"up": ["SELECT 1"], "down": null}', applied_id),
        )
        with self.assertRaises(ValueError) as caught:
            Migrator(self.conn, []).down()
        self.assertIn("has no down step", str(caught.exception))

    def test_a_row_with_unreadable_steps_is_not_revertible(self):
        Migrator(self.conn, []).up(models=self.models())
        self.conn.execute("UPDATE sustained_migrations SET steps = 'not json'")
        with self.assertRaises(ValueError) as caught:
            Migrator(self.conn, []).down()
        self.assertIn("not registered", str(caught.exception))

    def test_a_registered_migration_stores_no_steps(self):
        Migrator(
            self.conn, [Migration("001_t", up="CREATE TABLE gt (id INTEGER)")]
        ).up()
        (row,) = self.conn.execute("SELECT steps FROM sustained_migrations").fetchall()
        self.assertIsNone(row[0])

    def test_a_registered_migration_is_not_marked(self):
        migrator = Migrator(
            self.conn, [Migration("001_t", up="CREATE TABLE gt (id INTEGER)")]
        )
        migrator.up()
        self.assertFalse(migrator.applied_records()[0].generated)
        self.assertEqual(
            Migrator(self.conn, []).validate(raise_on_problems=False),
            ["applied migration '001_t' is not registered with this migrator"],
        )


class TestRehearsalProofs(MigrationTestCase):
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

    def test_a_clean_sweep_proves_the_schema_came_back(self):
        migrator = Migrator(
            self.conn,
            [
                Migration(
                    "001_t",
                    up="CREATE TABLE rp_t (id INTEGER)",
                    down="DROP TABLE rp_t",
                )
            ],
        )
        results = migrator.rehearse()
        self.assertEqual(results[0].reversed, [])
        self.assertIsNone(results[0].landed)

    def test_a_down_step_that_leaves_an_object_behind_is_reported(self):
        migrator = Migrator(
            self.conn,
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
        results = migrator.rehearse()
        self.assertTrue(results[0].down_ok)
        self.assertEqual(results[0].reversed, ["table 'rp_leftover' left behind"])

    def test_a_column_left_behind_is_reported(self):
        self.conn.execute("CREATE TABLE rp_users (id INTEGER)")
        migrator = Migrator(
            self.conn,
            [
                Migration(
                    "001_c",
                    up="ALTER TABLE rp_users ADD COLUMN bio TEXT",
                    down="SELECT 1",
                )
            ],
        )
        results = migrator.rehearse()
        self.assertEqual(results[0].reversed, ["column 'rp_users.bio' left behind"])

    def test_no_down_step_leaves_the_comparison_unchecked(self):
        migrator = Migrator(
            self.conn, [Migration("001_t", up="CREATE TABLE rp_t (id INTEGER)")]
        )
        results = migrator.rehearse()
        self.assertIsNone(results[0].reversed)

    def test_one_migration_without_a_down_step_spares_the_others(self):
        migrator = Migrator(
            self.conn,
            [
                Migration("001_k", up="CREATE TABLE rp_k (id INTEGER)"),
                Migration(
                    "002_t",
                    up="CREATE TABLE rp_t (id INTEGER)",
                    down="DROP TABLE rp_t",
                ),
            ],
        )
        results = migrator.rehearse()
        self.assertTrue(results[1].down_ok)
        self.assertIsNone(results[1].reversed)
        self.assertTrue(results.ok)

    def test_a_repeatable_still_allows_the_reversed_comparison(self):
        migrator = Migrator(
            self.conn,
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
        results = migrator.rehearse()
        self.assertTrue(results.ok)
        self.assertEqual(results[0].reversed, [])

    def test_a_rename_hint_survives_the_landed_check(self):
        self.conn.execute("CREATE TABLE rp_users (id INTEGER, mail VARCHAR(120))")
        migrator = Migrator(self.conn, [])
        results = migrator.rehearse(
            models=self.models(), renames={"rp_users.mail": "email"}
        )
        self.assertEqual(results[0].landed, [])
        self.assertTrue(results.ok)

    def test_a_table_rename_hint_survives_the_landed_check(self):
        self.conn.execute("CREATE TABLE rp_people (id INTEGER, email VARCHAR(120))")
        migrator = Migrator(self.conn, [])
        results = migrator.rehearse(
            models=self.models(), table_renames={"rp_people": "rp_users"}
        )
        self.assertEqual(results[0].landed, [])
        self.assertTrue(results.ok)

    def test_repeatables_rehearse_after_the_generated_migration(self):
        migrator = Migrator(
            self.conn,
            [
                Migration(
                    "vw_rp",
                    up="CREATE VIEW vw_rp AS SELECT id FROM rp_users",
                    repeatable=True,
                )
            ],
        )
        results = migrator.rehearse(models=self.models())
        self.assertTrue(results[0].id.startswith("auto_"))
        self.assertEqual(results[1].id, "vw_rp")
        self.assertTrue(results.ok)

    def test_models_rehearse_as_a_migration_of_their_own(self):
        migrator = Migrator(self.conn, [])
        results = migrator.rehearse(models=self.models())
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].id.startswith("auto_"))
        self.assertEqual(results[0].landed, [])
        self.assertEqual(results[0].reversed, [])
        self.assertNotIn("rp_users", table_names(self.conn))
        self.assertEqual(migrator.applied_records(), [])

    def test_the_generated_migration_runs_after_the_registered_ones(self):
        migrator = Migrator(
            self.conn,
            [
                Migration(
                    "001_t",
                    up="CREATE TABLE rp_t (id INTEGER)",
                    down="DROP TABLE rp_t",
                )
            ],
        )
        results = migrator.rehearse(models=self.models())
        self.assertEqual(results[0].id, "001_t")
        self.assertTrue(results[1].id.startswith("auto_"))
        self.assertIsNone(results[0].landed)

    def test_a_change_the_diff_skipped_is_not_reported_as_not_landed(self):
        self.conn.execute("CREATE TABLE rp_users (id INTEGER, email BOOLEAN)")
        migrator = Migrator(self.conn, [])
        results = migrator.rehearse(
            models=self.models(bio=String(20)), ignore_changed_columns=True
        )
        self.assertEqual(results[0].landed, [])
        self.assertTrue(results.ok)

    def test_models_that_match_the_database_rehearse_nothing(self):
        migrator = Migrator(self.conn, [])
        migrator.up(models=self.models())
        self.assertEqual(migrator.rehearse(models=self.models()), [])

    def test_a_generated_statement_that_fails_stops_the_rehearsal(self):
        # A view the models cannot see: introspection reports tables, so
        # the diff asks for a table the name is already taken by.
        self.conn.execute("CREATE TABLE rp_src (id INTEGER)")
        self.conn.execute("CREATE VIEW rp_users AS SELECT id FROM rp_src")
        migrator = Migrator(self.conn, [])
        results = migrator.rehearse(models=self.models(), migration_id="drift")
        self.assertEqual([(r.id, r.up_ok) for r in results], [("drift", False)])
        self.assertIn("rp_users", results[0].error)

    def test_a_migration_id_names_the_generated_migration(self):
        migrator = Migrator(self.conn, [])
        results = migrator.rehearse(models=self.models(), migration_id="drift")
        self.assertEqual(results[0].id, "drift")


class TestRehearsalKey(unittest.TestCase):
    """The key names content, not names or moments."""

    def test_the_same_statements_key_the_same_under_a_new_id(self):
        first = Migration("auto_1", up="CREATE TABLE k (id INTEGER)")
        second = Migration("auto_2", up="CREATE TABLE k (id INTEGER)")
        self.assertEqual(rehearsal_key([], [first]), rehearsal_key([], [second]))

    def test_different_statements_key_differently(self):
        first = Migration("one", up="CREATE TABLE k (id INTEGER)")
        second = Migration("one", up="CREATE TABLE k (id TEXT)")
        self.assertNotEqual(rehearsal_key([], [first]), rehearsal_key([], [second]))

    def test_the_applied_history_is_part_of_the_key(self):
        from sustained.migrations import AppliedRecord

        run = [Migration("one", up="CREATE TABLE k (id INTEGER)")]
        history = [AppliedRecord("older", 1, "abc", True)]
        self.assertNotEqual(rehearsal_key([], run), rehearsal_key(history, run))

    def test_a_failed_row_is_left_out_of_the_history(self):
        from sustained.migrations import AppliedRecord

        run = [Migration("one", up="CREATE TABLE k (id INTEGER)")]
        failed = [AppliedRecord("older", 1, "abc", False)]
        self.assertEqual(rehearsal_key([], run), rehearsal_key(failed, run))

    def test_a_callable_step_keys_on_its_id(self):
        run = [Migration("one", up=lambda conn: None)]
        same = [Migration("one", up=lambda conn: None)]
        other = [Migration("two", up=lambda conn: None)]
        self.assertEqual(rehearsal_key([], run), rehearsal_key([], same))
        self.assertNotEqual(rehearsal_key([], run), rehearsal_key([], other))


class CountingConnection:
    """A sqlite3 connection that counts the cursors it hands out and takes back."""

    def __init__(self, connection):
        self._connection = connection
        self.opened = 0
        self.closed = 0

    def cursor(self):
        self.opened += 1
        return CountingCursor(self, self._connection.cursor())

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


class CountingCursor:
    def __init__(self, owner, cursor):
        self._owner = owner
        self._cursor = cursor

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def close(self):
        self._owner.closed += 1
        self._cursor.close()


class TestCursorsAreGivenBack(unittest.TestCase):
    """
    A cursor holds its result set until it is closed, and pyodbc and the
    MySQL drivers refuse the next statement once enough of those pile up on
    one connection. Every cursor a run opens is closed again.
    """

    def setUp(self):
        self.raw = sqlite3.connect(":memory:")
        self.conn = CountingConnection(self.raw)
        self.addCleanup(self.raw.close)

    def migrator(self):
        return Migrator(
            self.conn,
            [
                Migration(
                    "0001",
                    up="CREATE TABLE cur_t (id INTEGER)",
                    down="DROP TABLE cur_t",
                ),
                Migration(
                    "0002",
                    up="CREATE TABLE cur_u (id INTEGER)",
                    down="DROP TABLE cur_u",
                ),
            ],
        )

    def test_a_full_run_closes_every_cursor(self):
        migrator = self.migrator()
        migrator.up()
        migrator.statuses()
        migrator.validate()
        migrator.repair()
        migrator.down(steps=2)
        self.assertEqual(self.conn.opened, self.conn.closed)
        self.assertGreater(self.conn.opened, 0)

    def test_a_baseline_closes_every_cursor(self):
        self.migrator().baseline("0002")
        self.assertEqual(self.conn.opened, self.conn.closed)

    def test_a_rehearsal_closes_every_cursor(self):
        self.migrator().rehearse()
        self.assertEqual(self.conn.opened, self.conn.closed)


class TestDestructivePrefixKeys(unittest.TestCase):
    """
    The keys a targeted run looks for. They are built one migration at a
    time, so the incremental build has to agree with a key computed for
    each slice on its own.
    """

    def key_for(self, start, stop):
        """The key of pending[start:stop] run after pending[:start]."""
        history = list(self.history)
        for seq, migration in enumerate(self.pending[:start], len(history) + 1):
            history.append(
                AppliedRecord(migration.id, seq, migration_checksum(migration), True)
            )
        return rehearsal_key(history, self.pending[start:stop])

    def setUp(self):
        self.history = [AppliedRecord("older", 1, "abc", True)]
        self.pending = [
            Migration("one", up="CREATE TABLE k (id INTEGER)"),
            Migration("two", up="CREATE TABLE j (id INTEGER)"),
            Migration("three", up="DROP TABLE k"),
            Migration("four", up="CREATE TABLE m (id INTEGER)"),
            Migration("five", up="DROP TABLE j"),
        ]

    def test_only_slices_that_remove_data_get_a_key(self):
        keys = _destructive_prefix_keys(self.history, self.pending)
        # 'three' and 'five' are the drops, so a slice earns a key once it
        # reaches one of them.
        expected = [
            self.key_for(start, stop)
            for start, stop in (
                (0, 3),
                (0, 4),
                (0, 5),
                (1, 3),
                (1, 4),
                (1, 5),
                (2, 3),
                (2, 4),
                (2, 5),
                (3, 5),
                (4, 5),
            )
        ]
        self.assertEqual(keys, expected)

    def test_a_key_is_recorded_once(self):
        keys = _destructive_prefix_keys(self.history, self.pending)
        self.assertEqual(len(keys), len(set(keys)))

    def test_a_run_that_removes_nothing_keys_nothing(self):
        keeping = [m for m in self.pending if "DROP" not in str(m.up)]
        self.assertEqual(_destructive_prefix_keys(self.history, keeping), [])

    def test_repeatables_are_left_out(self):
        pending = list(self.pending)
        pending.insert(0, Migration("seed", up="SELECT 1", repeatable=True))
        self.assertEqual(
            _destructive_prefix_keys(self.history, pending),
            _destructive_prefix_keys(self.history, self.pending),
        )

    def test_each_migration_renders_once(self):
        renders = []
        real = migrations_module.migration_sql

        def counted(migration, direction, compiler=None):
            renders.append(migration.id)
            return real(migration, direction, compiler)

        migrations_module.migration_sql = counted
        self.addCleanup(setattr, migrations_module, "migration_sql", real)
        _destructive_prefix_keys(self.history, self.pending)
        # Every migration is read once. A later start point needs to know
        # whether the migrations after it remove data, so the scan cannot
        # stop at the first drop.
        self.assertEqual(renders, ["one", "two", "three", "four", "five"])


class TestDuplicateMigrationIds(unittest.TestCase):
    def test_both_ids_are_named(self):
        runs = [
            Migration("one", up="SELECT 1"),
            Migration("two", up="SELECT 1"),
            Migration("one", up="SELECT 2"),
            Migration("two", up="SELECT 2"),
        ]
        with self.assertRaisesRegex(ValueError, r"\['one', 'two'\]"):
            checked_unique_ids(runs)

    def test_unique_ids_pass(self):
        self.assertIsNone(checked_unique_ids([Migration("one", up="SELECT 1")]))


class TestRenamedNames(unittest.TestCase):
    """The pre-2.20 names still import, with a warning."""

    def test_the_old_names_reach_the_current_ones(self):
        import sustained.migrations as module

        pairs = [
            ("receipt_key", module.rehearsal_key),
            ("RECEIPT_PASSED", module.REHEARSAL_PASSED),
            ("RECEIPT_FAILED", module.REHEARSAL_FAILED),
            ("RECEIPT_OVERRIDE", module.REHEARSAL_OVERRIDE),
        ]
        for old, current in pairs:
            with self.assertWarns(DeprecationWarning):
                self.assertIs(getattr(module, old), current)

    def test_an_unknown_name_still_raises(self):
        import sustained.migrations as module

        with self.assertRaises(AttributeError):
            module.no_such_name


class TestRehearsalRows(MigrationTestCase):
    """A rehearsal leaves a row the next run can read."""

    def migrations(self):
        return [
            Migration(
                "001_users",
                up="CREATE TABLE rc_users (id INTEGER)",
                down="DROP TABLE rc_users",
            )
        ]

    def test_a_passing_rehearsal_records_its_key(self):
        migrator = Migrator(self.conn, self.migrations())
        rehearsal = migrator.rehearse()
        self.assertTrue(rehearsal.ok)
        self.assertTrue(rehearsal.recorded)
        self.assertTrue(migrator.rehearsed(rehearsal.key))
        self.assertEqual(migrator.rehearsal_outcome(rehearsal.key), REHEARSAL_PASSED)

    def test_a_failing_rehearsal_records_the_failure(self):
        broken = Migration("002_bad", up="NOT SQL", down="DROP TABLE nothing")
        migrator = Migrator(self.conn, self.migrations() + [broken])
        rehearsal = migrator.rehearse()
        self.assertFalse(rehearsal.ok)
        self.assertEqual(migrator.rehearsal_outcome(rehearsal.key), REHEARSAL_FAILED)
        self.assertFalse(migrator.rehearsed(rehearsal.key))

    def test_an_unknown_key_has_no_outcome(self):
        migrator = Migrator(self.conn, self.migrations())
        self.assertIsNone(migrator.rehearsal_outcome("0" * 64))
        self.assertFalse(migrator.rehearsed("0" * 64))

    def test_a_second_rehearsal_replaces_the_row(self):
        migrator = Migrator(self.conn, self.migrations())
        key = migrator.rehearse().key
        migrator.record_rehearsal(key, REHEARSAL_FAILED)
        migrator.record_rehearsal(key, REHEARSAL_PASSED)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM sustained_rehearsals WHERE rehearsal_key = ?",
            (key,),
        ).fetchone()
        self.assertEqual(rows[0], 1)
        self.assertTrue(migrator.rehearsed(key))

    def test_an_unknown_outcome_is_refused(self):
        migrator = Migrator(self.conn, self.migrations())
        with self.assertRaises(ValueError) as caught:
            migrator.record_rehearsal("0" * 64, "maybe")
        self.assertIn("passed", str(caught.exception))

    def test_the_rehearsal_table_is_not_read_as_drift(self):
        migrator = Migrator(self.conn, [])
        migrator.up(models=[MigUser])
        migrator.record_rehearsal("0" * 64)
        self.assertIsNone(migrator.plan([MigUser], allow_drops=True))


class TestDestructiveGate(MigrationTestCase):
    """A run that removes data needs a rehearsal that proved it."""

    def setUp(self):
        super().setUp()
        self.conn.execute("CREATE TABLE gate_old (id INTEGER)")
        self.drop = Migration(
            "001_drop",
            up="DROP TABLE gate_old",
            down="CREATE TABLE gate_old (id INTEGER)",
        )

    def test_an_unrehearsed_drop_is_refused(self):
        migrator = Migrator(self.conn, [self.drop])
        with self.assertRaises(RehearsalRequired) as caught:
            migrator.up()
        message = str(caught.exception)
        self.assertIn("no rehearsal has proved these statements", message)
        self.assertIn("001_drop  DROP TABLE gate_old", message)
        self.assertIn("sustained rehearse", message)
        self.assertIn("--unrehearsed", message)
        self.assertEqual(migrator.applied(), [])

    def test_a_rehearsed_drop_runs(self):
        migrator = Migrator(self.conn, [self.drop])
        self.assertTrue(migrator.rehearse().ok)
        self.assertEqual(migrator.up(), ["001_drop"])
        self.assertNotIn("gate_old", table_names(self.conn))

    def test_the_override_runs_without_a_rehearsal_row(self):
        migrator = Migrator(self.conn, [self.drop])
        self.assertEqual(migrator.up(unrehearsed=True), ["001_drop"])

    def test_the_override_is_recorded_and_unlocks_nothing(self):
        migrator = Migrator(self.conn, [self.drop])
        migrator.up(unrehearsed=True)
        key = rehearsal_key(migrator.applied_records()[:0], [self.drop])
        self.assertEqual(migrator.rehearsal_outcome(key), "override")
        self.assertFalse(migrator.rehearsed(key))

    def test_an_additive_override_records_nothing(self):
        additive = Migration("001_add", up="CREATE TABLE gate_new (id INTEGER)")
        migrator = Migrator(self.conn, [additive])
        migrator.up(unrehearsed=True)
        self.assertIsNone(
            migrator.rehearsal_outcome(rehearsal_key([], [additive])),
        )

    def test_a_targeted_message_names_the_target(self):
        later = Migration("002_trim", up="DROP TABLE gate_old")
        migrator = Migrator(self.conn, [self.drop, later])
        with self.assertRaises(RehearsalRequired) as caught:
            migrator.up(target="001_drop")
        self.assertIn(
            "sustained migrate --target 001_drop --unrehearsed",
            str(caught.exception),
        )

    def test_a_block_after_the_registered_run_names_what_applied(self):
        registered = Migration(
            "001_add",
            up="CREATE TABLE gate_new (id INTEGER)",
            down="DROP TABLE gate_new",
        )
        migrator = Migrator(self.conn, [registered])
        with self.assertRaises(RehearsalRequired) as caught:
            migrator.up(models=[MigUser], allow_drops=True)
        self.assertEqual(getattr(caught.exception, "applied", None), ["001_add"])
        self.assertIn("gate_new", table_names(self.conn))

    def test_a_failed_rehearsal_reads_differently(self):
        broken = Migration("001_drop", up=["DROP TABLE gate_old", "NOT SQL"])
        migrator = Migrator(self.conn, [broken])
        self.assertFalse(migrator.rehearse().ok)
        with self.assertRaises(RehearsalRequired) as caught:
            migrator.up()
        self.assertIn(
            "The last rehearsal of these statements failed", str(caught.exception)
        )

    def test_editing_the_migration_voids_the_row(self):
        Migrator(self.conn, [self.drop]).rehearse()
        edited = Migration(
            "001_drop",
            up=["DROP TABLE gate_old", "CREATE TABLE gate_new (id INTEGER)"],
            down="DROP TABLE gate_new",
        )
        with self.assertRaises(RehearsalRequired):
            Migrator(self.conn, [edited]).up()

    def test_an_additive_run_is_never_gated(self):
        additive = Migration("001_add", up="CREATE TABLE gate_new (id INTEGER)")
        self.assertEqual(Migrator(self.conn, [additive]).up(), ["001_add"])

    def test_a_callable_step_cannot_trigger_the_gate(self):
        def drop(connection):
            connection.execute("DROP TABLE gate_old")

        migrator = Migrator(self.conn, [Migration("001_call", up=drop)])
        self.assertEqual(migrator.up(), ["001_call"])

    def test_a_generated_drop_is_gated_and_then_runs(self):
        migrator = Migrator(self.conn, [])
        with self.assertRaises(RehearsalRequired) as caught:
            migrator.up(models=[MigUser], allow_drops=True)
        self.assertIn("DROP TABLE", str(caught.exception))
        # The registered migrations that ran before the diff stay applied;
        # here there are none, and the tables the models want are absent.
        self.assertNotIn("mig_users", table_names(self.conn))
        self.assertTrue(
            Migrator(self.conn, []).rehearse(models=[MigUser], allow_drops=True).ok
        )
        Migrator(self.conn, []).up(models=[MigUser], allow_drops=True)
        self.assertIn("mig_users", table_names(self.conn))
        self.assertNotIn("gate_old", table_names(self.conn))

    def test_a_targeted_run_uses_the_prefix_the_rehearsal_proved(self):
        later = Migration(
            "002_add",
            up="CREATE TABLE gate_new (id INTEGER)",
            down="DROP TABLE gate_new",
        )
        migrator = Migrator(self.conn, [self.drop, later])
        self.assertTrue(migrator.rehearse().ok)
        self.assertEqual(migrator.up(target="001_drop"), ["001_drop"])
        self.assertNotIn("gate_old", table_names(self.conn))
        self.assertNotIn("gate_new", table_names(self.conn))

    def test_a_targeted_run_past_an_unrehearsed_drop_is_refused(self):
        later = Migration("002_trim", up="DROP TABLE gate_old")
        first = Migration(
            "001_add",
            up="CREATE TABLE gate_new (id INTEGER)",
            down="DROP TABLE gate_new",
        )
        migrator = Migrator(self.conn, [first, later])
        # The rehearsal covers both. Editing the second voids the prefix
        # that includes it, while the first still applies on its own.
        self.assertTrue(migrator.rehearse().ok)
        edited = Migrator(
            self.conn, [first, Migration("002_trim", up="DROP TABLE gate_old;")]
        )
        self.assertEqual(edited.up(target="001_add"), ["001_add"])
        with self.assertRaises(RehearsalRequired):
            edited.up(target="002_trim")

    def test_two_targeted_runs_in_a_row_use_one_rehearsal(self):
        second = Migration("002_trim", up="DROP TABLE gate_second")
        self.conn.execute("CREATE TABLE gate_second (id INTEGER)")
        migrator = Migrator(self.conn, [self.drop, second])
        self.assertTrue(migrator.rehearse().ok)
        # The second target starts from a history the first target wrote,
        # and the rehearsal passed through that state on its way up.
        self.assertEqual(migrator.up(target="001_drop"), ["001_drop"])
        self.assertEqual(migrator.up(target="002_trim"), ["002_trim"])
        self.assertNotIn("gate_second", table_names(self.conn))

    def test_a_rehearsal_with_models_also_covers_the_registered_set(self):
        migrator = Migrator(self.conn, [self.drop])
        rehearsal = migrator.rehearse(models=[MigUser])
        self.assertTrue(rehearsal.ok)
        # The rehearsal ran the drop and the generated migration; a run
        # without models applies the drop alone, which it also proved.
        self.assertEqual(Migrator(self.conn, [self.drop]).up(), ["001_drop"])


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestDuckdbMigrationRollback(unittest.TestCase):
    """
    On DuckDB every cursor is its own session, so a migration statement on
    a fresh cursor would commit outside the migration's transaction. The
    runner routes its statements through the transaction's own cursor, and
    a migration that fails halfway leaves no schema behind.
    """

    def test_a_failed_migration_leaves_no_schema_behind(self):
        conn = duckdb.connect(":memory:")
        bad = Migration(
            "001_bad",
            up=[
                "CREATE TABLE duck_things (id INTEGER)",
                "CREATE TABLE duck_things (id INTEGER)",
            ],
            down="DROP TABLE duck_things",
        )
        migrator = Migrator(conn, [bad], dialect=Dialects.DUCKDB)
        with self.assertRaises(Exception):
            migrator.up()
        tables = [
            row[0]
            for row in conn.cursor()
            .execute("SELECT table_name FROM information_schema.tables")
            .fetchall()
        ]
        self.assertNotIn("duck_things", tables)
        self.assertEqual([], migrator.validate(raise_on_problems=False))
        self.assertEqual([("001_bad", "pending")], migrator.statuses())


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestDuckdbRehearsalRollback(unittest.TestCase):
    """
    A rehearsal on DuckDB must take its own statements back. Its
    transaction lives on one cursor, and every rehearsed statement runs on
    that cursor, so the rollback reaches all of them.
    """

    def tables(self, conn):
        rows = (
            conn.cursor()
            .execute("SELECT table_name FROM information_schema.tables")
            .fetchall()
        )
        return {row[0] for row in rows}

    def test_a_passing_rehearsal_leaves_no_schema_behind(self):
        conn = duckdb.connect(":memory:")
        migration = Migration(
            "001_ducks",
            up="CREATE TABLE duck_rehearsal (id INTEGER)",
            down="DROP TABLE duck_rehearsal",
        )
        migrator = Migrator(conn, [migration], dialect=Dialects.DUCKDB)
        results = migrator.rehearse()
        self.assertEqual([(r.up_ok, r.down_ok) for r in results], [(True, True)])
        self.assertNotIn("duck_rehearsal", self.tables(conn))
        self.assertEqual([], migrator.applied_records())

    def test_a_failed_down_step_still_takes_the_up_ddl_back(self):
        conn = duckdb.connect(":memory:")
        migration = Migration(
            "001_ducks",
            up="CREATE TABLE duck_rehearsal (id INTEGER)",
            down="DROP TABLE duck_missing",
        )
        migrator = Migrator(conn, [migration], dialect=Dialects.DUCKDB)
        results = migrator.rehearse()
        self.assertEqual([(r.up_ok, r.down_ok) for r in results], [(True, False)])
        self.assertNotIn("duck_rehearsal", self.tables(conn))
        self.assertEqual(
            REHEARSAL_FAILED,
            migrator.rehearsal_outcome(rehearsal_key([], [migration])),
        )

    def test_the_rehearsal_reads_the_schema_it_just_changed(self):
        """
        The snapshot comparison and the models diff both introspect inside
        the rehearsal, so the read must share the transaction's cursor.
        """
        conn = duckdb.connect(":memory:")
        migration = Migration(
            "001_ducks",
            up="CREATE TABLE duck_rehearsal (id INTEGER)",
            down="DROP TABLE duck_rehearsal",
        )
        rehearsal = Migrator(conn, [migration], dialect=Dialects.DUCKDB).rehearse()
        self.assertEqual([], rehearsal[0].reversed)


if __name__ == "__main__":
    unittest.main()
