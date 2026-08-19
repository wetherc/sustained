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

from sustained.dialects import Dialects
from sustained.introspect import introspect_schema
from sustained.migrations import Migration, Migrator
from sustained.model import Model
from sustained.schema import Enum, Integer, Json, String, Text

from . import harness

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


class ServerCase(unittest.TestCase):
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

    def migrator(self, migrations=(), connection=None):
        return Migrator(
            connection or self.connection, list(migrations), dialect=self.DIALECT
        )

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
