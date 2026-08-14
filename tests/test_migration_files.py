"""
Tests for loading migrations from SQL files.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sustained.migration_files import (
    load_migrations,
    split_sql_statements,
    substitute_placeholders,
)
from sustained.migrations import Migrator, migration_checksum


class TestSplitSqlStatements(unittest.TestCase):
    def test_splits_on_line_ending_semicolons(self):
        text = "CREATE TABLE a (x INTEGER);\nCREATE TABLE b (y INTEGER);\n"
        self.assertEqual(
            split_sql_statements(text),
            ["CREATE TABLE a (x INTEGER)", "CREATE TABLE b (y INTEGER)"],
        )

    def test_final_semicolon_is_optional(self):
        self.assertEqual(
            split_sql_statements("SELECT 1;\nSELECT 2"),
            ["SELECT 1", "SELECT 2"],
        )

    def test_blank_pieces_are_dropped(self):
        self.assertEqual(split_sql_statements("\n\nSELECT 1;\n\n\n"), ["SELECT 1"])

    def test_mid_line_semicolons_do_not_split(self):
        text = "INSERT INTO t VALUES ('a;b');\n"
        self.assertEqual(split_sql_statements(text), ["INSERT INTO t VALUES ('a;b')"])


class LoaderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name, content):
        (self.dir / name).write_text(content, encoding="utf-8")


class TestLoadMigrations(LoaderTestCase):
    def test_loads_ordered_pairs(self):
        self.write("0002_add_flag.up.sql", "ALTER TABLE u ADD COLUMN flag INTEGER;\n")
        self.write("0002_add_flag.down.sql", "ALTER TABLE u DROP COLUMN flag;\n")
        self.write(
            "0001_create_users.up.sql",
            "CREATE TABLE u (id INTEGER);\nCREATE INDEX iu ON u (id);\n",
        )
        migrations = load_migrations(self.dir)
        self.assertEqual(
            [m.id for m in migrations], ["0001_create_users", "0002_add_flag"]
        )
        self.assertEqual(len(migrations[0].up), 2)
        self.assertIsNone(migrations[0].down)
        self.assertEqual(migrations[1].down, ["ALTER TABLE u DROP COLUMN flag"])

    def test_loaded_migrations_run(self):
        self.write("0001_create.up.sql", "CREATE TABLE t (id INTEGER);\n")
        self.write("0001_create.down.sql", "DROP TABLE t;\n")
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        migrator = Migrator(conn, load_migrations(self.dir))
        self.assertEqual(migrator.up(), ["0001_create"])
        self.assertEqual(migrator.validate(), [])
        self.assertEqual(migrator.down(), ["0001_create"])

    def test_missing_directory_raises(self):
        with self.assertRaises(ValueError):
            load_migrations(self.dir / "nope")

    def test_empty_up_file_raises(self):
        self.write("0001_x.up.sql", "\n-- nothing here\n")
        with self.assertRaisesRegex(ValueError, "no statements"):
            load_migrations(self.dir)

    def test_empty_down_file_raises(self):
        self.write("0001_x.up.sql", "SELECT 1;\n")
        self.write("0001_x.down.sql", "\n")
        with self.assertRaisesRegex(ValueError, "no statements"):
            load_migrations(self.dir)

    def test_orphaned_down_file_raises(self):
        self.write("0001_x.down.sql", "SELECT 1;\n")
        with self.assertRaisesRegex(ValueError, "without an up file"):
            load_migrations(self.dir)

    def test_misnamed_sql_file_raises(self):
        self.write("0001_x.sql", "SELECT 1;\n")
        with self.assertRaisesRegex(ValueError, "neither"):
            load_migrations(self.dir)

    def test_non_sql_files_are_ignored(self):
        self.write("0001_x.up.sql", "SELECT 1;\n")
        self.write("README.md", "notes")
        self.assertEqual([m.id for m in load_migrations(self.dir)], ["0001_x"])


class TestSubstitutePlaceholders(unittest.TestCase):
    def test_replaces_known_keys(self):
        self.assertEqual(
            substitute_placeholders(
                "GRANT SELECT ON t TO ${reader}", {"reader": "app_ro"}, "f.up.sql"
            ),
            "GRANT SELECT ON t TO app_ro",
        )

    def test_escape_produces_literal(self):
        self.assertEqual(
            substitute_placeholders("SELECT '$${x}'", None, "f.up.sql"),
            "SELECT '${x}'",
        )

    def test_escape_allowed_next_to_known_key(self):
        self.assertEqual(
            substitute_placeholders("$${a}${a}", {"a": "1"}, "f.up.sql"),
            "${a}1",
        )

    def test_unknown_key_raises_with_file_and_key(self):
        with self.assertRaisesRegex(ValueError, r"'f\.up\.sql'.*\$\{schema\}"):
            substitute_placeholders("SET search_path = ${schema}", {}, "f.up.sql")

    def test_non_identifier_braces_pass_through(self):
        text = "SELECT '${1bad}' || '${}'"
        self.assertEqual(substitute_placeholders(text, None, "f.up.sql"), text)


class TestLoaderPlaceholders(LoaderTestCase):
    def test_values_substitute_in_up_and_down(self):
        self.write("0001_grant.up.sql", "GRANT SELECT ON t TO ${reader};\n")
        self.write("0001_grant.down.sql", "REVOKE SELECT ON t FROM ${reader};\n")
        migrations = load_migrations(self.dir, placeholders={"reader": "app_ro"})
        self.assertEqual(migrations[0].up, ["GRANT SELECT ON t TO app_ro"])
        self.assertEqual(migrations[0].down, ["REVOKE SELECT ON t FROM app_ro"])

    def test_unknown_key_raises(self):
        self.write("0001_grant.up.sql", "GRANT SELECT ON t TO ${reader};\n")
        with self.assertRaisesRegex(ValueError, "0001_grant.up.sql"):
            load_migrations(self.dir)

    def test_checksum_covers_substituted_text(self):
        self.write("0001_grant.up.sql", "GRANT SELECT ON t TO ${reader};\n")
        first = load_migrations(self.dir, placeholders={"reader": "a"})[0]
        second = load_migrations(self.dir, placeholders={"reader": "b"})[0]
        self.assertNotEqual(migration_checksum(first), migration_checksum(second))


if __name__ == "__main__":
    unittest.main()
