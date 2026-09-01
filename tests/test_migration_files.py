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

    def test_splits_on_a_semicolon_with_a_trailing_comment(self):
        text = "SELECT 1; -- first\nSELECT 2;-- second\nSELECT 3;\t--third\n"
        self.assertEqual(
            split_sql_statements(text), ["SELECT 1", "SELECT 2", "SELECT 3"]
        )

    def test_a_comment_line_of_its_own_stays_with_its_statement(self):
        text = "-- makes t\nCREATE TABLE t (id INTEGER); -- done\nSELECT 1;\n"
        self.assertEqual(
            split_sql_statements(text),
            ["-- makes t\nCREATE TABLE t (id INTEGER)", "SELECT 1"],
        )

    def test_a_semicolon_inside_a_comment_does_not_split(self):
        text = "SELECT 1 -- and a; semicolon\nFROM t;\n"
        self.assertEqual(
            split_sql_statements(text), ["SELECT 1 -- and a; semicolon\nFROM t"]
        )


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

    def test_commented_statements_run_one_at_a_time(self):
        # sqlite3 refuses a string that holds two statements, so a file
        # whose semicolons carry trailing comments must still split.
        self.write(
            "0001_create.up.sql",
            "CREATE TABLE t (id INTEGER); -- the table\n"
            "INSERT INTO t VALUES (1); -- one row\n",
        )
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        migrator = Migrator(conn, load_migrations(self.dir))
        self.assertEqual(migrator.up(), ["0001_create"])
        self.assertEqual(conn.execute("SELECT id FROM t").fetchall(), [(1,)])

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
        with self.assertRaisesRegex(ValueError, "none of"):
            load_migrations(self.dir)

    def test_any_misnamed_file_raises(self):
        for name in ("0002_add.up.sq", "0002_add.up", "README.md", "0002_add.up.SQL"):
            with self.subTest(name=name):
                self.write("0001_x.up.sql", "SELECT 1;\n")
                self.write(name, "SELECT 1;\n")
                with self.assertRaisesRegex(ValueError, "none of"):
                    load_migrations(self.dir)
                (self.dir / name).unlink()

    def test_dotfiles_and_backup_files_are_ignored(self):
        self.write("0001_x.up.sql", "SELECT 1;\n")
        for name in (
            ".DS_Store",
            ".gitkeep",
            "0001_x.up.sql~",
            "0001_x.up.sql.bak",
            "0001_x.up.sql.orig",
            "0001_x.up.sql.swp",
            "0001_x.up.sql.swo",
            "0001_x.up.sql.tmp",
        ):
            self.write(name, "SELECT 2;\n")
        self.assertEqual([m.id for m in load_migrations(self.dir)], ["0001_x"])

    def test_subdirectories_are_ignored(self):
        self.write("0001_x.up.sql", "SELECT 1;\n")
        (self.dir / "archive").mkdir()
        (self.dir / "archive" / "0000_old.up.sql").write_text("SELECT 1;\n")
        self.assertEqual([m.id for m in load_migrations(self.dir)], ["0001_x"])


class TestRepeatFiles(LoaderTestCase):
    def test_repeatables_load_after_versioned(self):
        self.write("zz_view.repeat.sql", "CREATE VIEW v AS SELECT 1;\n")
        self.write("0001_t.up.sql", "CREATE TABLE t (id INTEGER);\n")
        self.write("aa_seed.repeat.sql", "DELETE FROM t;\nINSERT INTO t VALUES (1);\n")
        migrations = load_migrations(self.dir)
        self.assertEqual([m.id for m in migrations], ["0001_t", "aa_seed", "zz_view"])
        self.assertEqual([m.repeatable for m in migrations], [False, True, True])
        self.assertIsNone(migrations[2].down)

    def test_up_and_repeat_for_same_id_raises(self):
        self.write("0001_t.up.sql", "SELECT 1;\n")
        self.write("0001_t.repeat.sql", "SELECT 1;\n")
        with self.assertRaisesRegex(ValueError, "both an up file and a repeat file"):
            load_migrations(self.dir)

    def test_empty_repeat_file_raises(self):
        self.write("v.repeat.sql", "-- nothing\n")
        with self.assertRaisesRegex(ValueError, "no statements"):
            load_migrations(self.dir)

    def test_down_next_to_repeat_is_orphaned(self):
        self.write("v.repeat.sql", "SELECT 1;\n")
        self.write("v.down.sql", "SELECT 1;\n")
        with self.assertRaisesRegex(ValueError, "without an up file"):
            load_migrations(self.dir)

    def test_placeholders_fill_repeat_files(self):
        self.write("v.repeat.sql", "CREATE VIEW ${name} AS SELECT 1;\n")
        migrations = load_migrations(self.dir, placeholders={"name": "v1"})
        self.assertEqual(migrations[0].up, ["CREATE VIEW v1 AS SELECT 1"])


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
            substitute_placeholders("SELECT '$${x}'", {}, "f.up.sql"),
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

    def test_none_mapping_leaves_text_untouched(self):
        text = "SELECT '${anything}' || '${1bad}' || '${ key }' || '${open"
        self.assertEqual(substitute_placeholders(text, None, "f.up.sql"), text)

    def test_dash_in_key_raises_with_file_and_snippet(self):
        with self.assertRaisesRegex(ValueError, r"'f\.up\.sql'.*\$\{my-key\}"):
            substitute_placeholders("SELECT ${my-key}", {}, "f.up.sql")

    def test_spaces_in_key_raise(self):
        with self.assertRaisesRegex(ValueError, "malformed"):
            substitute_placeholders("SELECT ${ key }", {"key": "1"}, "f.up.sql")

    def test_digit_first_key_raises(self):
        with self.assertRaisesRegex(ValueError, "malformed"):
            substitute_placeholders("SELECT ${1abc}", {}, "f.up.sql")

    def test_unclosed_marker_at_end_raises(self):
        with self.assertRaisesRegex(ValueError, r"malformed.*\$\{key"):
            substitute_placeholders("SELECT ${key", {"key": "1"}, "f.up.sql")


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
            load_migrations(self.dir, placeholders={})

    def test_no_mapping_leaves_files_untouched(self):
        self.write("0001_grant.up.sql", "GRANT SELECT ON t TO ${reader};\n")
        migrations = load_migrations(self.dir)
        self.assertEqual(migrations[0].up, ["GRANT SELECT ON t TO ${reader}"])

    def test_no_mapping_leaves_malformed_markers_untouched(self):
        self.write("0001_x.up.sql", "SELECT '${my-key}' || '${ key }' || '${open;\n")
        migrations = load_migrations(self.dir)
        self.assertEqual(
            migrations[0].up, ["SELECT '${my-key}' || '${ key }' || '${open"]
        )

    def test_malformed_marker_raises_with_file_name(self):
        self.write("0001_x.up.sql", "GRANT SELECT ON t TO ${my-key};\n")
        with self.assertRaisesRegex(ValueError, r"0001_x\.up\.sql.*malformed"):
            load_migrations(self.dir, placeholders={})

    def test_checksum_covers_substituted_text(self):
        self.write("0001_grant.up.sql", "GRANT SELECT ON t TO ${reader};\n")
        first = load_migrations(self.dir, placeholders={"reader": "a"})[0]
        second = load_migrations(self.dir, placeholders={"reader": "b"})[0]
        self.assertNotEqual(migration_checksum(first), migration_checksum(second))


if __name__ == "__main__":
    unittest.main()
