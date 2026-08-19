"""
Migrations loaded from SQL files, part of the `migrations` cover.

A temporary directory holds an up/down pair and a repeatable, written the
way a project would write them: `${key}` placeholders in the SQL, filled
at load time. Four claims are proven per server: the pair applies and
reverts, the repeatable re-runs when its contents change and not before,
baseline() adopts a database that already matches, and script('up')
renders the run without executing anything.

ServerCase in lifecycle.py mixes this in, so every server that claims
the `migrations` cover runs these tests.
"""

import tempfile
from pathlib import Path

from sustained.migration_files import load_migrations

CREATE_EVENTS = "CREATE TABLE it_events (id INT NOT NULL, label VARCHAR(${width}))"

PLACEHOLDERS = {"width": "40", "flavor": "tart"}


class FileMigrationTests:
    """Mixed into lifecycle.ServerCase; uses its helpers and fixtures."""

    def write_pair(self, directory):
        Path(directory, "0001_events.up.sql").write_text(
            CREATE_EVENTS + ";\n", encoding="utf-8"
        )
        Path(directory, "0001_events.down.sql").write_text(
            "DROP TABLE it_events;\n", encoding="utf-8"
        )

    def write_seed(self, directory, value):
        Path(directory, "seed.repeat.sql").write_text(
            "DELETE FROM it_events;\n"
            f"INSERT INTO it_events (id, label) VALUES (1, {value});\n",
            encoding="utf-8",
        )

    def labels(self):
        cursor = self.connection.cursor()
        cursor.execute("SELECT label FROM it_events")
        return [row[0] for row in cursor.fetchall()]

    def test_a_file_pair_applies_and_reverts(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_pair(directory)
            migrations = load_migrations(directory, PLACEHOLDERS)

            migrator = self.migrator(migrations)
            self.assertEqual(["0001_events"], migrator.up())
            self.assertEqual({"id", "label"}, set(self.tables()["it_events"].columns))

            self.assertEqual(["0001_events"], migrator.down())
            self.assertNotIn("it_events", self.tables())

    def test_a_repeatable_reruns_only_when_its_contents_change(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_pair(directory)
            self.write_seed(directory, "'${flavor}'")
            migrations = load_migrations(directory, PLACEHOLDERS)

            migrator = self.migrator(migrations)
            self.assertEqual(["0001_events", "seed"], migrator.up())
            # The placeholder value reached the server, not the marker.
            self.assertEqual(["tart"], self.labels())

            # Unchanged contents: nothing re-runs.
            self.assertEqual([], migrator.up())

            self.write_seed(directory, "'plum'")
            migrator = self.migrator(load_migrations(directory, PLACEHOLDERS))
            self.assertEqual(["seed"], migrator.up())
            self.assertEqual(["plum"], self.labels())

    def test_baseline_adopts_a_database_that_already_matches(self):
        self.execute(CREATE_EVENTS.replace("${width}", "40"))
        with tempfile.TemporaryDirectory() as directory:
            self.write_pair(directory)
            migrator = self.migrator(load_migrations(directory, PLACEHOLDERS))

            self.assertEqual(["0001_events"], migrator.baseline("0001_events"))
            self.assertEqual([], migrator.up())
            self.assertEqual([], migrator.validate(raise_on_problems=False))
            self.assertEqual([("0001_events", "applied")], migrator.statuses())

    def test_script_renders_the_run_without_executing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_pair(directory)
            migrator = self.migrator(load_migrations(directory, PLACEHOLDERS))

            script = migrator.script("up")
            self.assertIn("CREATE TABLE it_events", script)
            self.assertIn("sustained_migrations", script)

            self.assertNotIn("it_events", self.tables())
            self.assertEqual([], migrator.applied())
