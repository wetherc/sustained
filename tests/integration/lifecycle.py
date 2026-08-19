"""
The migration lifecycle, run the same way against every server that takes
it. A server module subclasses ServerCase, names its row in support.json,
and says which of the optional behaviours it has.

What runs here is what the support page calls `migrations`: apply the
models, read the schema back, take it down, rehearse, validate, repair, and
hold the advisory lock while two migrators run at once. The read surface
lives in queries.py, under the `queries` cover.
"""

import threading
import unittest

from sustained.analysis import destructive_statements
from sustained.dialects import Dialects
from sustained.exceptions import GuardBlocked, MigrationError, RehearsalRequired
from sustained.guards import no_drops
from sustained.introspect import introspect_schema
from sustained.migrations import Migration, Migrator
from sustained.model import Model
from sustained.schema import Enum, Integer, Json, String, Text

from . import harness
from .column_types import ColumnTypeTests

TABLES = (
    "it_widgets",
    "it_widgets_copy",
    "it_ctas",
    "it_events",
    "it_lock_a",
    "it_lock_b",
)
TRACKING = ("sustained_migrations", "sustained_rehearsals")


def widget_columns():
    return {
        "id": Integer(primary_key=True),
        "name": String(80, nullable=False),
        "size": Integer(),
    }


def make_model(class_name, table, dialect, columns):
    """A model class of its own, so one server cannot alter another's."""
    return type(
        class_name,
        (Model,),
        {"tableName": table, "tableColumns": columns, "_dialect": dialect},
    )


def statements_of(migration):
    """The up step of a generated migration, always as a list of strings."""
    step = migration.up
    return [step] if isinstance(step, str) else list(step)


def drop_everything(connection, dialect):
    """Leaves the database as the test found it."""
    compiler = Dialects.get_compiler(dialect)
    cursor = connection.cursor()
    for table in TABLES + TRACKING:
        cursor.execute(f"DROP TABLE IF EXISTS {compiler.quote_identifier(table)}")
    if compiler.enum_strategy() == "native":
        cursor.execute(compiler.compile_drop_enum_type("it_mood", if_exists=True))
    if hasattr(connection, "commit"):
        connection.commit()


class ServerCase(ColumnTypeTests, unittest.TestCase):
    """
    Base for one server. Subclasses set NAME to a row in support.json and
    DIALECT to the dialect that row names.
    """

    NAME = ""
    DIALECT = Dialects.DEFAULT
    REHEARSES_IN_PLACE = True
    HAS_ADVISORY_LOCK = False

    @classmethod
    def setUpClass(cls):
        if not cls.NAME:
            raise unittest.SkipTest("base class")
        cls.connection = harness.connect(cls.NAME)

    @classmethod
    def tearDownClass(cls):
        connection = getattr(cls, "connection", None)
        if connection is not None:
            connection.close()

    def setUp(self):
        self.Widget = make_model("Widget", "it_widgets", self.DIALECT, widget_columns())
        self.Widget.bind(self.connection)
        drop_everything(self.connection, self.DIALECT)

    def tearDown(self):
        drop_everything(self.connection, self.DIALECT)
        self.Widget.unbind()

    def migrator(self, migrations=(), connection=None, guards=None):
        return Migrator(
            connection or self.connection,
            list(migrations),
            dialect=self.DIALECT,
            guards=guards,
        )

    def execute(self, sql, params=()):
        cursor = self.connection.cursor()
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        if hasattr(self.connection, "commit"):
            self.connection.commit()

    def tables(self, connection=None):
        return introspect_schema(connection or self.connection, self.DIALECT)

    # The models

    def test_the_models_land_and_come_back(self):
        migrator = self.migrator()
        migrator.up(models=[self.Widget])

        table = self.tables()["it_widgets"]
        self.assertEqual({"id", "name", "size"}, set(table.columns))
        self.assertEqual(["applied"], [state for _, state in migrator.statuses()])

        migrator.down()
        self.assertNotIn("it_widgets", self.tables())

    def test_only_the_difference_is_applied(self):
        migrator = self.migrator()
        migrator.up(models=[self.Widget])

        self.Widget.tableColumns["note"] = Text()
        generated = migrator.plan([self.Widget])
        self.assertIsNotNone(generated)
        self.assertNotIn("CREATE TABLE", " ".join(statements_of(generated)).upper())

        migrator.up(models=[self.Widget])
        self.assertIn("note", set(self.tables()["it_widgets"].columns))

    def test_validate_and_repair_find_nothing_after_a_clean_run(self):
        migrator = self.migrator()
        migrator.up(models=[self.Widget])
        self.assertEqual([], migrator.validate(raise_on_problems=False))
        self.assertEqual([], migrator.repair())

    # Registered migrations

    def registered(self):
        quote = Dialects.get_compiler(self.DIALECT).quote_identifier
        events = quote("it_events")
        return [
            Migration(
                "001_events",
                up=f"CREATE TABLE {events} (id INT NOT NULL, label VARCHAR(40))",
                down=f"DROP TABLE {events}",
            )
        ]

    def test_a_registered_migration_runs_and_reverts(self):
        migrator = self.migrator(self.registered())
        self.assertEqual(["001_events"], migrator.up())
        self.assertIn("it_events", self.tables())

        self.assertEqual(["001_events"], migrator.down())
        self.assertNotIn("it_events", self.tables())

    # Rehearsal

    def test_rehearse_leaves_the_database_unchanged(self):
        if not self.REHEARSES_IN_PLACE:
            self.skipTest(f"{self.NAME} rehearses on a scratch database")
        rehearsal = self.migrator(self.registered()).rehearse()

        self.assertTrue(rehearsal.ok)
        for result in rehearsal:
            self.assertEqual([], result.reversed)
        self.assertNotIn("it_events", self.tables())

    def test_rehearse_refuses_where_the_schema_does_not_roll_back(self):
        if self.REHEARSES_IN_PLACE:
            self.skipTest(f"{self.NAME} rehearses in place")
        with self.assertRaises(ValueError) as raised:
            self.migrator(self.registered()).rehearse()
        self.assertIn("not on that list", str(raised.exception))

    def test_rehearse_runs_on_a_scratch_database(self):
        if self.REHEARSES_IN_PLACE:
            self.skipTest(f"{self.NAME} rehearses in place")
        scratch = harness.connect_scratch(self.NAME)
        try:
            drop_everything(scratch, self.DIALECT)
            rehearsal = self.migrator(self.registered(), connection=scratch).rehearse(
                scratch=True
            )
            self.assertTrue(rehearsal.ok)
        finally:
            drop_everything(scratch, self.DIALECT)
            scratch.close()

    # Validation and repair

    def tamper_checksum(self, migration_id):
        """Rewrites a stored checksum as an out-of-band edit would."""
        compiler = Dialects.get_compiler(self.DIALECT)
        quote = compiler.quote_identifier
        self.execute(
            f"UPDATE {quote('sustained_migrations')} "
            f"SET {quote('checksum')} = {compiler.placeholder()} "
            f"WHERE {quote('id')} = {compiler.placeholder()}",
            ("0" * 64, migration_id),
        )

    def test_validate_names_an_edited_migration_and_repair_accepts_it(self):
        migrator = self.migrator(self.registered())
        migrator.up()
        self.tamper_checksum("001_events")

        problems = migrator.validate(raise_on_problems=False)
        self.assertTrue(any("001_events" in problem for problem in problems))

        actions = migrator.repair()
        self.assertTrue(
            any("checksum" in action and "001_events" in action for action in actions)
        )
        self.assertEqual([], migrator.validate(raise_on_problems=False))

    def test_validate_refuses_an_out_of_order_migration(self):
        quote = Dialects.get_compiler(self.DIALECT).quote_identifier
        first, second = [
            Migration(
                migration_id,
                up=f"CREATE TABLE {quote(table)} (id INT NOT NULL)",
                down=f"DROP TABLE {quote(table)}",
            )
            for migration_id, table in (
                ("001_first", "it_lock_a"),
                ("002_second", "it_lock_b"),
            )
        ]
        self.migrator([second]).up()

        migrator = self.migrator([first, second])
        problems = migrator.validate(raise_on_problems=False)
        self.assertTrue(any("001_first" in problem for problem in problems))
        with self.assertRaises(MigrationError):
            migrator.up()

        self.assertEqual(["001_first"], migrator.up(allow_out_of_order=True))
        self.assertIn("it_lock_a", self.tables())

    def test_a_failed_migration_is_repaired_and_rerun(self):
        quote = Dialects.get_compiler(self.DIALECT).quote_identifier
        events = quote("it_events")
        create = f"CREATE TABLE {events} (id INT NOT NULL)"
        broken = Migration(
            "001_events", up=[create, "THIS IS NOT SQL"], down=f"DROP TABLE {events}"
        )
        migrator = self.migrator([broken])
        with self.assertRaises(Exception):
            migrator.up()

        if Dialects.get_compiler(self.DIALECT).supports_transactional_ddl():
            # The whole migration rolled back; nothing to repair.
            self.assertNotIn("it_events", self.tables())
            self.assertEqual([], migrator.validate(raise_on_problems=False))
        else:
            # The first statement stayed applied and a failure row landed.
            problems = migrator.validate(raise_on_problems=False)
            self.assertTrue(any("001_events" in problem for problem in problems))
            actions = migrator.repair()
            self.assertTrue(any("failed" in action for action in actions))
            self.assertEqual([], migrator.validate(raise_on_problems=False))
            self.execute(f"DROP TABLE IF EXISTS {events}")

        fixed = Migration("001_events", up=create, down=f"DROP TABLE {events}")
        rerun = self.migrator([fixed])
        self.assertEqual(["001_events"], rerun.up())
        self.assertIn("it_events", self.tables())

    # Guards and the rehearsal gate

    def test_no_drops_blocks_before_any_statement_runs(self):
        quote = Dialects.get_compiler(self.DIALECT).quote_identifier
        events = quote("it_events")
        create = Migration(
            "001_events",
            up=f"CREATE TABLE {events} (id INT NOT NULL)",
            down=f"DROP TABLE {events}",
        )
        self.migrator([create]).up()

        drop = Migration("002_drop", up=f"DROP TABLE {events}")
        guarded = self.migrator([create, drop], guards=[no_drops()])
        with self.assertRaises(GuardBlocked):
            guarded.up()

        self.assertIn("it_events", self.tables())
        self.assertNotIn("002_drop", guarded.applied())

    def test_a_rehearsed_drop_proceeds_with_allow_drops(self):
        if not self.REHEARSES_IN_PLACE:
            self.skipTest(f"{self.NAME} rehearses on a scratch database")
        migrator = self.migrator()
        migrator.up(models=[self.Widget])
        self.Widget.query().insert([{"id": 1, "name": "hinge", "size": 3}]).run()

        del self.Widget.tableColumns["size"]
        with self.assertRaises(RehearsalRequired):
            migrator.up(models=[self.Widget], allow_drops=True)
        self.assertIn("size", set(self.tables()["it_widgets"].columns))

        rehearsal = migrator.rehearse(models=[self.Widget], allow_drops=True)
        self.assertTrue(rehearsal.ok)
        migrator.up(models=[self.Widget], allow_drops=True)

        self.assertNotIn("size", set(self.tables()["it_widgets"].columns))
        row = self.Widget.query().where("id", "=", 1).orderBy("id").first()
        self.assertEqual("hinge", row.name)

    # Model diff safety rails

    def test_a_not_null_add_backfills_existing_rows(self):
        migrator = self.migrator()
        migrator.up(models=[self.Widget])
        self.Widget.query().insert([{"id": 1, "name": "hinge", "size": 3}]).run()

        self.Widget.tableColumns["grade"] = String(12, nullable=False, backfill="raw")
        generated = migrator.plan([self.Widget])
        if destructive_statements(statements_of(generated)):
            # SQLite spells the change as a table rebuild, which drops the
            # old table, so the rehearsal gate asks for proof first.
            self.assertTrue(migrator.rehearse(models=[self.Widget]).ok)
        migrator.up(models=[self.Widget])

        row = self.Widget.query().where("id", "=", 1).orderBy("id").first()
        self.assertEqual("raw", row.grade)
        self.assertIsNone(migrator.plan([self.Widget]))

    def test_a_rename_hint_keeps_the_data(self):
        migrator = self.migrator()
        migrator.up(models=[self.Widget])
        self.Widget.query().insert([{"id": 1, "name": "hinge", "size": 3}]).run()

        columns = widget_columns()
        columns["width"] = columns.pop("size")
        renamed = make_model("WidgetRenamed", "it_widgets", self.DIALECT, columns)
        renamed.bind(self.connection)
        try:
            migrator.up(models=[renamed], renames={"it_widgets.size": "width"})
            self.assertIsNone(migrator.plan([renamed]))
            row = renamed.query().where("id", "=", 1).orderBy("id").first()
            self.assertEqual(3, row.width)
        finally:
            renamed.unbind()

    # Concurrency

    def test_two_migrators_run_at_once_under_the_advisory_lock(self):
        if not self.HAS_ADVISORY_LOCK:
            self.skipTest(f"{self.NAME} has no advisory lock")
        quote = Dialects.get_compiler(self.DIALECT).quote_identifier
        migrations = [
            Migration(
                table,
                up=f"CREATE TABLE {quote(table)} (id INT NOT NULL)",
                down=f"DROP TABLE {quote(table)}",
            )
            for table in ("it_lock_a", "it_lock_b")
        ]
        failures = []

        def run():
            connection = harness.connect(self.NAME)
            try:
                self.migrator(migrations, connection=connection).up()
            except Exception as error:  # reported on the main thread
                failures.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual([], failures)
        tables = self.tables()
        self.assertIn("it_lock_a", tables)
        self.assertIn("it_lock_b", tables)

    # Drift

    def test_a_json_column_does_not_drift_against_its_own_ddl(self):
        """
        MariaDB stores JSON as longtext with a check constraint, and MySQL
        reports its own type spellings. A column that reads back as
        something else would make every run generate the same migration.
        """
        self.Widget.tableColumns["payload"] = Json()
        migrator = self.migrator()
        migrator.up(models=[self.Widget])
        self.assertIsNone(migrator.plan([self.Widget]))

    def test_an_enum_column_round_trips(self):
        """
        The enum rendering the dialect chooses (a named type, an inline
        list, or a check constraint) has to read back as the same column,
        or every later run would regenerate the same migration.
        """
        strategy = Dialects.get_compiler(self.DIALECT).enum_strategy()
        self.Widget.tableColumns["mood"] = Enum("sad", "ok", "great", name="it_mood")
        migrator = self.migrator()
        migrator.up(models=[self.Widget])

        snapshot = self.tables()
        self.assertIn("mood", snapshot["it_widgets"].columns)
        if strategy == "native" and snapshot.enum_types_read:
            # Postgres reads pg_enum; the type and its values come back.
            self.assertEqual(("sad", "ok", "great"), snapshot.enum_types.get("it_mood"))
        self.assertIsNone(migrator.plan([self.Widget]))

        self.Widget.query().insert(
            [{"id": 1, "name": "hinge", "size": 3, "mood": "ok"}]
        ).run()
        row = self.Widget.query().where("id", "=", 1).orderBy("id").first()
        self.assertEqual("ok", row.mood)

        migrator.down()
        self.assertNotIn("it_widgets", self.tables())
