"""
Tests for the command-line migration runner.

Each test writes a config module and a migrations directory into a temp
directory, points the CLI at them, and calls main() directly.
"""

import contextlib
import io
import json
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


class CliBase(unittest.TestCase):
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


class CliTestCase(CliBase):
    """The commands that run, revert, and inspect migrations."""

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
        name = f"empty_ph_config_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(CONFIG_TEMPLATE + "\nplaceholders = {}\n")
        self.addCleanup(sys.modules.pop, name, None)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["status", "--config", name])
        self.assertEqual(code, 1)
        self.assertIn("placeholder", stderr.getvalue())

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


class PlanCliTestCase(CliBase):
    """The plan command: pending work, problems, and model drift."""

    def _config(self, suffix, extra):
        name = f"plan_config_{suffix}_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(CONFIG_TEMPLATE + extra)
        self.addCleanup(sys.modules.pop, name, None)
        return name

    def _run(self, name, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([*argv, "--config", name])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_pending_work_exits_two(self):
        code, out, _ = self.run_cli("plan")
        self.assertEqual(code, 2)
        self.assertIn("pending", out)
        self.assertIn("001_users", out)
        self.assertIn("1 statement", out)
        self.assertIn("2 pending migrations", out)
        self.assertIn("run: sustained migrate", out)

    def test_nothing_to_do_exits_zero(self):
        self.run_cli("migrate")
        code, out, _ = self.run_cli("plan")
        self.assertEqual(code, 0)
        self.assertIn("Nothing pending, no problems.", out)
        self.assertIn("names no models", out)

    def test_destructive_statements_are_labelled(self):
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "003_cleanup.up.sql",
            "DROP TABLE flags;",
        )
        code, out, _ = self.run_cli("plan")
        self.assertEqual(code, 2)
        self.assertIn("destructive  DROP TABLE flags", out)

    def test_problems_win_over_pending(self):
        self.run_cli("migrate")
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "001_users.up.sql",
            "CREATE TABLE users (id BIGINT);",
        )
        code, out, _ = self.run_cli("plan")
        self.assertEqual(code, 1)
        self.assertIn("problems", out)
        self.assertIn("checksum mismatch", out)
        self.assertNotIn("run: sustained migrate", out)

    def test_pending_and_problems_print_as_two_sections(self):
        self.run_cli("migrate", "--target", "001_users")
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "001_users.up.sql",
            "CREATE TABLE users (id BIGINT);",
        )
        code, out, _ = self.run_cli("plan")
        self.assertEqual(code, 1)
        self.assertIn("pending\n  002_flag", out)
        self.assertIn("\n\nproblems\n", out)
        self.assertIn("1 pending migration, 1 problem", out)

    def test_pending_and_drift_print_as_two_sections(self):
        name = self._config(
            "both",
            "\nfrom sustained import create_model\n"
            "from sustained.schema import Integer\n"
            "Users = create_model('Users', 'users')\n"
            "Users.tableColumns = {'id': Integer()}\n"
            "Users.columns = ('id',)\n"
            "models = [Users]\n",
        )
        code, out, _ = self._run(name, "plan")
        self.assertEqual(code, 2)
        self.assertIn("\n\ndrift\n", out)
        self.assertIn("CREATE TABLE users", out)
        self.assertIn("2 pending migrations, 1 drift statement", out)

    def test_changed_repeatable_is_marked(self):
        migrations = os.path.join(self.dir.name, "migrations")
        self._write(
            migrations, "active.repeat.sql", "CREATE VIEW IF NOT EXISTS v AS SELECT 1;"
        )
        self.run_cli("migrate")
        self._write(
            migrations, "active.repeat.sql", "CREATE VIEW IF NOT EXISTS v AS SELECT 2;"
        )
        code, out, _ = self.run_cli("plan")
        self.assertEqual(code, 2)
        self.assertIn("active", out)
        self.assertIn("repeat changed", out)

    def test_new_repeatable_is_marked(self):
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "active.repeat.sql",
            "CREATE VIEW IF NOT EXISTS v AS SELECT 1;",
        )
        code, out, _ = self.run_cli("plan")
        self.assertEqual(code, 2)
        self.assertIn("1 statement  repeat", out)

    def test_callable_step_counts_nothing(self):
        name = self._config(
            "callable",
            "\nfrom sustained.migrations import Migration\n"
            "def _backfill(connection):\n    connection.execute('SELECT 1')\n"
            "migrations = [Migration('000_backfill', up=_backfill, "
            "checksum='fixed')]\n",
        )
        code, out, _ = self._run(name, "plan")
        self.assertEqual(code, 2)
        self.assertIn("000_backfill  callable step", out)

    def test_drift_reported_when_models_configured(self):
        name = self._config(
            "models",
            "\nfrom sustained import create_model\n"
            "from sustained.schema import Integer, Text\n"
            "Drifted = create_model('Drifted', 'users')\n"
            "Drifted.tableColumns = {'id': Integer(primary_key=True), "
            "'bio': Text()}\n"
            "Drifted.columns = ('id', 'bio')\n"
            "models = [Drifted]\n",
        )
        self._run(name, "migrate")
        code, out, _ = self._run(name, "plan")
        self.assertEqual(code, 2)
        self.assertIn("drift", out)
        self.assertIn("ADD COLUMN bio", out)
        self.assertIn("DROP TABLE flags", out)
        self.assertIn("2 drift statements", out)
        self.assertNotIn("run: sustained migrate", out)

    def test_no_drift_when_models_match(self):
        name = self._config(
            "matching",
            "\nfrom sustained import create_model\n"
            "from sustained.schema import Integer\n"
            "Users = create_model('Users', 'users')\n"
            "Users.tableColumns = {'id': Integer()}\n"
            "Users.columns = ('id',)\n"
            "Flags = create_model('Flags', 'flags')\n"
            "Flags.tableColumns = {'id': Integer()}\n"
            "Flags.columns = ('id',)\n"
            "models = [Users, Flags]\n",
        )
        self._run(name, "migrate")
        code, out, _ = self._run(name, "plan")
        self.assertEqual(code, 0)
        self.assertIn("Nothing pending, no problems, no drift.", out)


class JsonOutputTestCase(CliBase):
    """The --json flag on status, validate, and plan."""

    def _json(self, *argv):
        code, out, err = self.run_cli(*argv, "--json")
        return code, json.loads(out), err

    def test_status_lists_every_state(self):
        self.run_cli("migrate", "--target", "001_users")
        code, payload, _ = self._json("status")
        self.assertEqual(code, 0)
        self.assertEqual(
            payload,
            {
                "migrations": [
                    {"id": "001_users", "state": "applied"},
                    {"id": "002_flag", "state": "pending"},
                ]
            },
        )

    def test_validate_ok(self):
        self.run_cli("migrate")
        code, payload, _ = self._json("validate")
        self.assertEqual(code, 0)
        self.assertEqual(payload, {"ok": True, "problems": []})

    def test_validate_reports_problems(self):
        self.run_cli("migrate")
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "001_users.up.sql",
            "CREATE TABLE users (id BIGINT);",
        )
        code, payload, _ = self._json("validate")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(len(payload["problems"]), 1)
        self.assertIn("checksum mismatch", payload["problems"][0])

    def test_plan_lists_pending_work(self):
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "003_cleanup.up.sql",
            "DROP TABLE flags;",
        )
        code, payload, _ = self._json("plan")
        self.assertEqual(code, 2)
        self.assertEqual(payload["problems"], [])
        self.assertIsNone(payload["drift"])
        self.assertEqual(
            payload["pending"][2],
            {
                "id": "003_cleanup",
                "state": "pending",
                "repeatable": False,
                "statements": 1,
                "destructive": ["DROP TABLE flags"],
            },
        )

    def test_plan_clean_run(self):
        self.run_cli("migrate")
        code, payload, _ = self._json("plan")
        self.assertEqual(code, 0)
        self.assertEqual(payload, {"pending": [], "problems": [], "drift": None})

    def test_plan_problems_win(self):
        self.run_cli("migrate")
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "001_users.up.sql",
            "CREATE TABLE users (id BIGINT);",
        )
        code, payload, _ = self._json("plan")
        self.assertEqual(code, 1)
        self.assertEqual(len(payload["problems"]), 1)

    def test_plan_callable_step_has_null_statements(self):
        name = f"json_config_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(
                CONFIG_TEMPLATE + "\nfrom sustained.migrations import Migration\n"
                "def _backfill(connection):\n    connection.execute('SELECT 1')\n"
                "migrations = [Migration('000_backfill', up=_backfill, "
                "checksum='fixed')]\n"
            )
        self.addCleanup(sys.modules.pop, name, None)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["plan", "--json", "--config", name])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertIsNone(payload["pending"][0]["statements"])
        self.assertEqual(payload["pending"][0]["destructive"], [])

    def test_plan_drift_is_a_list_when_models_are_named(self):
        name = f"json_models_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(
                CONFIG_TEMPLATE + "\nfrom sustained import create_model\n"
                "from sustained.schema import Integer, Text\n"
                "Drifted = create_model('Drifted', 'users')\n"
                "Drifted.tableColumns = {'id': Integer(primary_key=True), "
                "'bio': Text()}\n"
                "Drifted.columns = ('id', 'bio')\n"
                "models = [Drifted]\n"
            )
        self.addCleanup(sys.modules.pop, name, None)
        with contextlib.redirect_stdout(io.StringIO()):
            main(["migrate", "--config", name])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["plan", "--json", "--config", name])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["pending"], [])
        self.assertEqual(len(payload["drift"]), 2)
        self.assertTrue(any("ADD COLUMN bio" in s for s in payload["drift"]))


if __name__ == "__main__":
    unittest.main()
