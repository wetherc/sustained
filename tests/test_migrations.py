"""
Tests for the migration runner against in-memory SQLite.

These cover what only Migrator does, and the helpers around it: checksums,
rehearsal keys, driver switches, and callable steps that take a connection.
The behaviour both runners share is tested once, against each, in
test_migrator_behaviour.py.
"""

import sqlite3
import unittest
from unittest import mock

try:
    import duckdb

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False

import sustained.migrations.migrator as migrator_module
import sustained.migrations.rehearsal as rehearsal_module
from sustained import Model
from sustained.ddl import drop_table
from sustained.dialects import Dialects
from sustained.exceptions import MigrationError
from sustained.execution import _set_quietly, legacy_sqlite_control
from sustained.migrations import (
    REHEARSAL_FAILED,
    AppliedRecord,
    Migration,
    Migrator,
    _destructive_prefix_keys,
    _legacy_checksum,
    checked_unique_ids,
    migration_checksum,
    rehearsal_key,
)
from sustained.schema import Integer


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
    def test_callable_step(self):
        seen = []

        def make_it(conn):
            conn.execute("CREATE TABLE cb_tbl (id INTEGER)")
            seen.append(True)

        migrator = Migrator(self.conn, [Migration("cb", up=make_it)])
        migrator.up()
        self.assertTrue(seen)
        self.assertIn("cb_tbl", table_names(self.conn))


class TestChecksums(unittest.TestCase):
    def test_string_and_list_steps_hash_the_same_sql(self):
        one = Migration("m", up="  CREATE TABLE t (id INTEGER)  ")
        two = Migration("m", up=["CREATE TABLE t (id INTEGER)"])
        self.assertEqual(migration_checksum(one), migration_checksum(two))

    def test_changed_sql_changes_the_checksum(self):
        one = Migration("m", up="CREATE TABLE t (id INTEGER)")
        two = Migration("m", up="CREATE TABLE t (id BIGINT)")
        self.assertNotEqual(migration_checksum(one), migration_checksum(two))

    def test_splitting_a_statement_changes_the_checksum(self):
        joined = Migration("m", up=["CREATE TABLE a (id INTEGER);\nSELECT 1"])
        split = Migration("m", up=["CREATE TABLE a (id INTEGER);", "SELECT 1"])
        self.assertNotEqual(migration_checksum(joined), migration_checksum(split))
        # The newline-joined hash cannot tell the two apart.
        self.assertEqual(_legacy_checksum(joined), _legacy_checksum(split))

    def test_a_ddl_step_and_its_signature_as_sql_hash_differently(self):
        step = drop_table("t")
        as_sql = Migration("m", up=[step.signature()], down=None)
        self.assertNotEqual(
            migration_checksum(Migration("m", up=[step], down=None)),
            migration_checksum(as_sql),
        )

    def test_the_legacy_checksum_keeps_an_explicit_or_missing_one(self):
        pinned = Migration("m", up=lambda c: None, checksum="abc123")
        self.assertEqual(_legacy_checksum(pinned), "abc123")
        self.assertIsNone(_legacy_checksum(Migration("m", up=lambda c: None)))
        ddl = Migration("m", up=[drop_table("t")], down=None)
        self.assertNotEqual(_legacy_checksum(ddl), migration_checksum(ddl))

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
    def rows(self):
        return self.conn.execute(
            "SELECT id, seq, checksum, applied_at, execution_ms, success "
            "FROM sustained_migrations ORDER BY seq"
        ).fetchall()

    def test_callable_step_records_null_checksum(self):
        Migrator(
            self.conn,
            [Migration("cb", up=lambda c: c.execute("CREATE TABLE cbt (x INTEGER)"))],
        ).up()
        (row,) = self.rows()
        self.assertIsNone(row[2])


class TestValidateAndRepair(MigrationTestCase):
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


class RebuiltParent(Model):
    """A table whose column type change forces a SQLite rebuild."""

    tableName = "reb_parent"
    tableColumns = {"id": Integer(primary_key=True), "code": Integer()}


class TestSqliteRebuildPragmas(MigrationTestCase):
    """
    A generated rebuild turns foreign key enforcement off and on again.
    SQLite ignores both pragmas inside a transaction, so the migration
    runs bare, and a stock sqlite3 connection needs its own implicit
    transaction turned off for that.
    """

    def setUp(self):
        super().setUp()
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("CREATE TABLE reb_parent (id INTEGER PRIMARY KEY, code TEXT)")
        self.conn.execute(
            "CREATE TABLE reb_child (id INTEGER PRIMARY KEY, "
            "parent_id INTEGER REFERENCES reb_parent(id))"
        )
        self.conn.execute("INSERT INTO reb_parent (id, code) VALUES (1, '7')")
        self.conn.commit()

    def run_rebuild(self):
        return Migrator(self.conn, []).up(models=[RebuiltParent], unrehearsed=True)

    def test_the_rebuild_runs_and_keeps_the_rows(self):
        self.assertEqual(len(self.run_rebuild()), 1)
        rows = self.conn.execute("SELECT id, code FROM reb_parent").fetchall()
        self.assertEqual(rows, [(1, 7)])

    def test_enforcement_is_on_again_afterwards(self):
        self.run_rebuild()
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_an_orphan_row_is_refused_afterwards(self):
        self.run_rebuild()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO reb_child (id, parent_id) VALUES (1, 999)")

    def test_the_implicit_transaction_comes_back(self):
        before = self.conn.isolation_level
        self.run_rebuild()
        self.assertEqual(self.conn.isolation_level, before)


class TestLegacySqliteDetection(unittest.TestCase):
    """
    The switch is decided by the connection's class, not by the module
    name it reports, so a connection made with a factory is detected too.
    """

    def test_a_factory_subclass_is_detected(self):
        class MyConnection(sqlite3.Connection):
            pass

        conn = sqlite3.connect(":memory:", factory=MyConnection)
        self.addCleanup(conn.close)
        self.assertNotEqual(type(conn).__module__.partition(".")[0], "sqlite3")
        self.assertTrue(legacy_sqlite_control(conn))

    def test_a_plain_connection_is_detected(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertTrue(legacy_sqlite_control(conn))

    def test_another_driver_is_left_alone(self):
        class Psycopg2Connection:
            autocommit = False

        self.assertFalse(legacy_sqlite_control(Psycopg2Connection()))


class RefusingIsolationConnection:
    """A connection whose driver refuses to take its switch back."""

    def __setattr__(self, name, value):
        raise RuntimeError("cannot set isolation_level")


class TestIsolationRestore(unittest.TestCase):
    def test_a_refused_restore_is_dropped(self):
        _set_quietly(RefusingIsolationConnection(), "isolation_level", "")


class TestRepeatableMigrations(MigrationTestCase):
    def test_repeatable_rejects_down_step(self):
        with self.assertRaisesRegex(ValueError, "down step"):
            Migration("r", up="SELECT 1", down="SELECT 2", repeatable=True)

    def test_repeatable_callable_requires_checksum(self):
        with self.assertRaisesRegex(ValueError, "checksum"):
            Migration("r", up=lambda conn: None, repeatable=True)
        Migration("r", up=lambda conn: None, checksum="abc", repeatable=True)


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

    def test_rehearse_refuses_inside_an_open_transaction(self):
        from sustained.execution import transaction

        migrator = Migrator(self.conn, self.migrations())
        with transaction(self.conn):
            with self.assertRaises(ValueError) as caught:
                migrator.rehearse()
        self.assertIn("open transaction()", str(caught.exception))


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

    def test_a_failed_generated_migration_is_not_kept(self):
        migrator = Migrator(self.conn, [])
        models = self.models()
        real_run_step = migrator_module._run_step

        def refuse(connection, step, compiler):
            raise RuntimeError("no")

        with mock.patch.object(migrator_module, "_run_step", refuse):
            with self.assertRaises(RuntimeError):
                migrator.up(models=models)
        # The failed migration is gone from the list, so the next run
        # diffs the models again instead of repeating the failed SQL.
        self.assertEqual(migrator._migrations, [])
        applied = migrator.up(models=models)
        self.assertEqual(len(applied), 1)
        self.assertIn("gen_users", table_names(self.conn))
        self.assertIs(migrator_module._run_step, real_run_step)


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

    def tail_key(self, start, repeatables):
        """The key of an untargeted run after pending[:start] applied."""
        history = list(self.history)
        for seq, migration in enumerate(self.pending[:start], len(history) + 1):
            history.append(
                AppliedRecord(migration.id, seq, migration_checksum(migration), True)
            )
        return rehearsal_key(history, self.pending[start:] + repeatables)

    def test_a_repeatable_extends_only_the_untargeted_run(self):
        seed = Migration("seed", up="SELECT 1", repeatable=True)
        keys = _destructive_prefix_keys(self.history, [seed] + self.pending)
        targeted = _destructive_prefix_keys(self.history, self.pending)
        # Every start point before 'five' reaches a drop, so its tail with
        # the repeatable gets a key. The start point after 'five' runs only
        # the repeatable, which removes nothing.
        tails = [self.tail_key(start, [seed]) for start in range(5)]
        self.assertEqual(set(keys), set(targeted) | set(tails))
        self.assertEqual(len(keys), len(targeted) + len(tails))
        self.assertNotIn(self.tail_key(5, [seed]), keys)

    def test_a_repeatable_that_removes_data_keys_every_tail(self):
        self.pending = [m for m in self.pending if "DROP" not in str(m.up)]
        purge = Migration("purge", up="DELETE FROM k", repeatable=True)
        keys = _destructive_prefix_keys(self.history, self.pending + [purge])
        self.assertEqual(keys, [self.tail_key(start, [purge]) for start in range(4)])

    def test_each_migration_renders_once(self):
        renders = []
        real = rehearsal_module.migration_sql

        def counted(migration, direction, compiler=None):
            renders.append(migration.id)
            return real(migration, direction, compiler)

        rehearsal_module.migration_sql = counted
        self.addCleanup(setattr, rehearsal_module, "migration_sql", real)
        _destructive_prefix_keys(self.history, self.pending)
        # Every migration is read once. A later start point needs to know
        # whether the migrations after it remove data, so the scan cannot
        # stop at the first drop.
        self.assertEqual(renders, ["one", "two", "three", "four", "five"])

    def test_each_checksum_is_computed_once(self):
        computed = []
        real = rehearsal_module.migration_checksum

        def counted(migration):
            computed.append(migration.id)
            return real(migration)

        rehearsal_module.migration_checksum = counted
        self.addCleanup(setattr, rehearsal_module, "migration_checksum", real)
        _destructive_prefix_keys(self.history, self.pending)
        # The slice loops walk every (start, end) pair, so an uncached
        # checksum would be recomputed hundreds of times on a long run.
        self.assertEqual(computed, ["one", "two", "three", "four", "five"])


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

    def test_a_callable_step_cannot_trigger_the_gate(self):
        def drop(connection):
            connection.execute("DROP TABLE gate_old")

        migrator = Migrator(self.conn, [Migration("001_call", up=drop)])
        self.assertEqual(migrator.up(), ["001_call"])


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
