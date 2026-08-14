"""
Tests for the command-line migration runner.

Each test writes a config module and a migrations directory into a temp
directory, points the CLI at them, and calls main() directly.
"""

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest

from sustained.cli import main

CONFIG_TEMPLATE = """
import os
import sqlite3

def get_connection():
    return sqlite3.connect(os.path.join(os.path.dirname(__file__), "cli.db"))

migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
"""


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config_name = f"cli_config_{id(self)}"
        with open(os.path.join(self.dir.name, f"{self.config_name}.py"), "w") as f:
            f.write(CONFIG_TEMPLATE)
        migrations = os.path.join(self.dir.name, "migrations")
        os.mkdir(migrations)
        self._write(migrations, "001_users.up.sql", "CREATE TABLE users (id INTEGER);")
        self._write(migrations, "001_users.down.sql", "DROP TABLE users;")
        self._write(migrations, "002_flag.up.sql", "CREATE TABLE flags (id INTEGER);")
        self._write(migrations, "002_flag.down.sql", "DROP TABLE flags;")
        self._old_cwd = os.getcwd()
        os.chdir(self.dir.name)
        self.addCleanup(os.chdir, self._old_cwd)
        self.addCleanup(sys.modules.pop, self.config_name, None)

    def _write(self, directory, name, text):
        with open(os.path.join(directory, name), "w") as f:
            f.write(text)

    def run_cli(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([*argv, "--config", self.config_name])
        return code, stdout.getvalue(), stderr.getvalue()

    def db(self):
        return sqlite3.connect(os.path.join(self.dir.name, "cli.db"))

    def table_names(self):
        with contextlib.closing(self.db()) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {r[0] for r in rows}

    def test_migrate_then_status(self):
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("applied  001_users", out)
        self.assertIn("applied  002_flag", out)
        self.assertIn("users", self.table_names())
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("applied  001_users", out)
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to apply.", out)

    def test_migrate_target_stops_early(self):
        code, out, _ = self.run_cli("migrate", "--target", "001_users")
        self.assertEqual(code, 0)
        self.assertNotIn("002_flag", out)
        self.assertNotIn("flags", self.table_names())

    def test_down_reverts_newest(self):
        self.run_cli("migrate")
        code, out, _ = self.run_cli("down")
        self.assertEqual(code, 0)
        self.assertIn("reverted 002_flag", out)
        self.assertNotIn("flags", self.table_names())

    def test_down_to_target(self):
        self.run_cli("migrate")
        code, out, _ = self.run_cli("down", "--to", "001_users")
        self.assertEqual(code, 0)
        self.assertIn("reverted 002_flag", out)
        self.assertIn("users", self.table_names())

    def test_validate_reports_problems_and_exit_code(self):
        self.run_cli("migrate")
        code, out, _ = self.run_cli("validate")
        self.assertEqual(code, 0)
        self.assertIn("OK", out)
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "001_users.up.sql",
            "CREATE TABLE users (id BIGINT);",
        )
        code, out, _ = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("checksum mismatch", out)

    def test_repair_accepts_edit(self):
        self.run_cli("migrate")
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "001_users.up.sql",
            "CREATE TABLE users (id BIGINT);",
        )
        code, out, _ = self.run_cli("repair")
        self.assertEqual(code, 0)
        self.assertIn("updated the stored checksum of '001_users'", out)
        code, _, _ = self.run_cli("validate")
        self.assertEqual(code, 0)

    def test_script_renders_without_applying(self):
        code, out, _ = self.run_cli("script")
        self.assertEqual(code, 0)
        self.assertIn("CREATE TABLE users", out)
        self.assertNotIn("users", self.table_names())
        code, out, _ = self.run_cli("script", "down")
        self.assertEqual(code, 0)

    def test_baseline_records_without_running(self):
        code, out, _ = self.run_cli("baseline", "001_users")
        self.assertEqual(code, 0)
        self.assertIn("baselined 001_users", out)
        self.assertNotIn("users", self.table_names())
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("applied  002_flag", out)
        self.assertNotIn("001_users", out)

    def test_migration_error_exits_one(self):
        self.run_cli("migrate")
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "001_users.up.sql",
            "CREATE TABLE users (id BIGINT);",
        )
        code, _, err = self.run_cli("migrate")
        self.assertEqual(code, 1)
        self.assertIn("checksum mismatch", err)

    def test_unknown_target_exits_one(self):
        code, _, err = self.run_cli("migrate", "--target", "nope")
        self.assertEqual(code, 1)
        self.assertIn("Unknown migration target", err)

    def test_missing_config_exits_one(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["status", "--config", "does_not_exist_xyz"])
        self.assertEqual(code, 1)
        self.assertIn("error:", stderr.getvalue())

    def test_config_without_connection_exits_one(self):
        name = f"bad_config_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write("migrations = []\n")
        self.addCleanup(sys.modules.pop, name, None)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["status", "--config", name])
        self.assertEqual(code, 1)
        self.assertIn("connection", stderr.getvalue())

    def test_setup_failure_closes_connection(self):
        name = f"leak_config_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(
                CONFIG_TEMPLATE + "\nconnections = []\n"
                "_original = get_connection\n"
                "def get_connection():\n"
                "    conn = _original()\n"
                "    connections.append(conn)\n"
                "    return conn\n"
                "migrations_dir = 'does_not_exist_dir'\n"
            )
        self.addCleanup(sys.modules.pop, name, None)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["status", "--config", name])
        self.assertEqual(code, 1)
        connections = sys.modules[name].connections
        self.assertEqual(len(connections), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")

    def test_dialect_name_resolves(self):
        name = f"dialect_config_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(CONFIG_TEMPLATE + "\ndialect = 'default'\n")
        self.addCleanup(sys.modules.pop, name, None)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["status", "--config", name])
        self.assertEqual(code, 0)

    def test_repeatable_runs_and_status_reports_changed(self):
        migrations = os.path.join(self.dir.name, "migrations")
        self._write(
            migrations, "active.repeat.sql", "CREATE VIEW IF NOT EXISTS v AS SELECT 1;"
        )
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("applied  active", out)
        self._write(
            migrations, "active.repeat.sql", "CREATE VIEW IF NOT EXISTS v AS SELECT 2;"
        )
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("changed  active", out)
        with contextlib.closing(self.db()) as conn:
            conn.execute("DROP VIEW v")
            conn.commit()
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("applied  active", out)
        code, out, _ = self.run_cli("status")
        self.assertIn("applied  active", out)

    def test_placeholders_from_config(self):
        migrations = os.path.join(self.dir.name, "migrations")
        self._write(
            migrations, "003_extra.up.sql", "CREATE TABLE ${extra} (id INTEGER);"
        )
        name = f"ph_config_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(CONFIG_TEMPLATE + "\nplaceholders = {'extra': 'extras'}\n")
        self.addCleanup(sys.modules.pop, name, None)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["migrate", "--config", name])
        self.assertEqual(code, 0)
        self.assertIn("extras", self.table_names())

    def test_missing_placeholder_exits_one(self):
        migrations = os.path.join(self.dir.name, "migrations")
        self._write(
            migrations, "003_extra.up.sql", "CREATE TABLE ${extra} (id INTEGER);"
        )
        code, _, err = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("placeholder", err)

    def test_unknown_dialect_exits_one(self):
        name = f"bad_dialect_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(CONFIG_TEMPLATE + "\ndialect = 'oracle'\n")
        self.addCleanup(sys.modules.pop, name, None)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["status", "--config", name])
        self.assertEqual(code, 1)
        self.assertIn("Unknown dialect", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
