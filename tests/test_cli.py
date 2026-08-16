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
from unittest import mock

from sustained.cli import main
from sustained.migrations import Migrator

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

    def test_a_connection_that_will_not_open_reports_an_error(self):
        name = f"broken_config_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(
                "import sqlite3\n\n"
                "def get_connection():\n"
                "    return sqlite3.connect('/nowhere/at/all/app.db')\n"
            )
        self.addCleanup(sys.modules.pop, name, None)
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(stderr):
                code = main(["status", "--config", name])
        self.assertEqual(code, 1)
        self.assertIn("error: unable to open database file", stderr.getvalue())

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
        self.assertNotIn("run: Migrator.sync(models)", out)

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
        self.assertIn("run: sustained migrate", out)
        self.assertNotIn("Migrator.sync", out)

    def test_drift_that_only_drops_offers_no_migrate(self):
        name = self._config(
            "drops",
            "\nfrom sustained import create_model\n"
            "from sustained.schema import Integer\n"
            "Users = create_model('Users', 'users')\n"
            "Users.tableColumns = {'id': Integer()}\n"
            "Users.columns = ('id',)\n"
            "models = [Users]\n",
        )
        self._run(name, "migrate")
        code, out, _ = self._run(name, "plan")
        self.assertEqual(code, 2)
        self.assertIn("DROP TABLE flags", out)
        self.assertNotIn("run: sustained migrate", out)
        self.assertIn("migrate does not generate drops", out)

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
        code, out, _ = self._run(name, "plan")
        self.assertEqual(code, 2)
        self.assertIn("drift", out)
        self.assertIn("CREATE TABLE users", out)
        self.assertIn("run: sustained migrate", out)

        # migrate closes the drift it can generate; the drop it will not
        # generate stays, and plan keeps reporting it.
        self._run(name, "migrate")
        code, out, _ = self._run(name, "plan")
        self.assertEqual(code, 2)
        self.assertNotIn("ADD COLUMN bio", out)
        self.assertIn("DROP TABLE flags", out)
        self.assertIn("1 drift statement", out)

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
                "statements": [{"sql": "DROP TABLE flags", "destructive": True}],
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
        self.assertEqual(len(payload["drift"]), 1)
        self.assertEqual(payload["drift"][0]["destructive"], True)
        self.assertIn("DROP TABLE flags", payload["drift"][0]["sql"])
        self.assertNotIn("Migrator.sync", stdout.getvalue())
        self.assertEqual(set(payload), {"pending", "problems", "drift"})


class RehearseCliTestCase(CliBase):
    """`sustained rehearse` and the scratch database escape hatch."""

    def test_rehearse_reports_both_directions_and_changes_nothing(self):
        code, out, _ = self.run_cli("rehearse")
        self.assertEqual(code, 0)
        self.assertIn("rehearsed 001_users  up ok, down ok, reversed", out)
        self.assertIn("rehearsed 002_flag   up ok, down ok, reversed", out)
        self.assertIn("rollback complete, database unchanged", out)
        self.assertEqual(
            self.table_names(),
            {"sustained_migrations", "sustained_rehearsals"},
        )

    def test_rehearse_reports_a_broken_migration_and_exits_1(self):
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "003_bad.up.sql",
            "CRATE TABLE oops (id INTEGER);",
        )
        code, out, _ = self.run_cli("rehearse")
        self.assertEqual(code, 1)
        self.assertIn("failed    003_bad", out)
        self.assertIn("syntax error", out)
        self.assertIn("rollback complete, database unchanged", out)
        self.assertEqual(
            self.table_names(),
            {"sustained_migrations", "sustained_rehearsals"},
        )

    def test_a_migration_without_a_down_step_is_not_a_failure(self):
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "003_notes.up.sql",
            "CREATE TABLE notes (id INTEGER);",
        )
        code, out, _ = self.run_cli("rehearse")
        self.assertEqual(code, 0)
        self.assertIn("rehearsed 003_notes  up ok, no down step", out)
        self.assertIn("down not reached: '003_notes' has no down step", out)

    def test_a_failing_down_step_exits_1(self):
        migrations = os.path.join(self.dir.name, "migrations")
        self._write(migrations, "003_bad.up.sql", "CREATE TABLE bd (id INTEGER);")
        self._write(migrations, "003_bad.down.sql", "DROP TABLE missing_table;")
        code, out, _ = self.run_cli("rehearse")
        self.assertEqual(code, 1)
        self.assertIn("down failed", out)

    def test_nothing_pending_rehearses_nothing(self):
        self.run_cli("migrate")
        code, out, _ = self.run_cli("rehearse")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to rehearse.", out)

    def test_a_down_step_that_leaves_an_object_behind_exits_1(self):
        migrations = os.path.join(self.dir.name, "migrations")
        self._write(migrations, "003_pair.up.sql", "CREATE TABLE kept (id INTEGER);")
        self._write(
            migrations, "004_extra.up.sql", "CREATE TABLE leftover (id INTEGER);"
        )
        self._write(migrations, "004_extra.down.sql", "SELECT 1;")
        self._write(migrations, "003_pair.down.sql", "DROP TABLE kept;")
        code, out, _ = self.run_cli("rehearse")
        self.assertEqual(code, 1)
        self.assertIn("up ok, down ok, not reversed", out)
        self.assertIn("leftover     table 'leftover' left behind", out)
        self.assertIn("run: sustained plan", out)
        self.assertEqual(
            self.table_names(),
            {"sustained_migrations", "sustained_rehearsals"},
        )

    def test_models_are_rehearsed_with_the_pending_migrations(self):
        name = f"rehearse_models_{id(self)}"
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
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["rehearse", "--config", name])
        out = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("rehearsed 001_users", out)
        self.assertIn("up ok, landed, down ok, reversed", out)
        self.assertEqual(
            self.table_names(),
            {"sustained_migrations", "sustained_rehearsals"},
        )

    def test_rehearse_json_reports_every_check(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["rehearse", "--json", "--config", self.config_name])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(
            set(payload), {"rehearsed", "scratch", "key", "recorded", "ok"}
        )
        self.assertTrue(payload["recorded"])
        self.assertEqual(len(payload["key"]), 64)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["scratch"])
        self.assertEqual(
            payload["rehearsed"][0],
            {
                "id": "001_users",
                "up_ok": True,
                "down_ok": True,
                "error": None,
                "landed": None,
                "reversed": [],
            },
        )

    def test_a_dialect_that_cannot_roll_back_is_refused(self):
        name = f"athena_config_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(CONFIG_TEMPLATE + "\ndialect = 'athena'\n")
        self.addCleanup(sys.modules.pop, name, None)
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(stderr):
                code = main(["rehearse", "--config", name])
        self.assertEqual(code, 1)
        self.assertIn("athena is not on that list", stderr.getvalue())
        self.assertIn("get_rehearsal_connection()", stderr.getvalue())

    def test_a_scratch_connection_runs_the_whole_history_there(self):
        name = f"scratch_config_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(
                CONFIG_TEMPLATE + "\n"
                "def get_rehearsal_connection():\n"
                "    return sqlite3.connect(\n"
                "        os.path.join(os.path.dirname(__file__), 'scratch.db')\n"
                "    )\n"
            )
        self.addCleanup(sys.modules.pop, name, None)
        with contextlib.redirect_stdout(io.StringIO()):
            main(["migrate", "--config", name])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["rehearse", "--config", name])
        out = stdout.getvalue()
        self.assertEqual(code, 0)
        # Nothing is pending on the real database, but the scratch one is
        # empty, so the whole history rehearses there.
        self.assertIn("rehearsed 001_users", out)
        self.assertIn("rehearsal complete on the scratch database", out)
        self.assertIn("users", self.table_names())
        with contextlib.closing(
            sqlite3.connect(os.path.join(self.dir.name, "scratch.db"))
        ) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        self.assertEqual({r[0] for r in rows}, {"sustained_migrations"})


class ReceiptCliTestCase(CliBase):
    """`migrate` asks for the proof `rehearse` left behind."""

    def setUp(self):
        super().setUp()
        self.run_cli("migrate")
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "003_trim.up.sql",
            "DROP TABLE flags;",
        )
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "003_trim.down.sql",
            "CREATE TABLE flags (id INTEGER);",
        )

    def test_migrate_refuses_an_unrehearsed_drop(self):
        code, out, err = self.run_cli("migrate")
        self.assertEqual(code, 1)
        self.assertIn("no rehearsal has proved these statements", err)
        self.assertIn("003_trim  DROP TABLE flags", err)
        self.assertIn("--unrehearsed", err)
        self.assertIn("flags", self.table_names())

    def test_rehearse_then_migrate_applies_it(self):
        code, out, _ = self.run_cli("rehearse")
        self.assertEqual(code, 0)
        self.assertIn("receipt recorded", out)
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("applied  003_trim", out)
        self.assertNotIn("flags", self.table_names())

    def test_the_override_applies_it_without_a_rehearsal(self):
        code, out, _ = self.run_cli("migrate", "--unrehearsed")
        self.assertEqual(code, 0)
        self.assertNotIn("flags", self.table_names())

    def test_plan_points_at_rehearse_when_a_drop_is_waiting(self):
        code, out, _ = self.run_cli("plan")
        self.assertEqual(code, 2)
        self.assertIn("destructive  DROP TABLE flags", out)
        self.assertIn("run: sustained rehearse", out)
        self.assertNotIn("run: sustained migrate", out)

    def test_an_edit_after_the_rehearsal_voids_the_receipt(self):
        self.run_cli("rehearse")
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "003_trim.up.sql",
            "DROP TABLE flags;\nDROP TABLE users;",
        )
        code, _, err = self.run_cli("migrate")
        self.assertEqual(code, 1)
        self.assertIn("no rehearsal has proved these statements", err)

    def test_a_scratch_run_that_misses_a_pending_migration_records_nothing(self):
        name = f"scratch_partial_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(
                CONFIG_TEMPLATE + "\n"
                "def get_rehearsal_connection():\n"
                "    return sqlite3.connect(\n"
                "        os.path.join(os.path.dirname(__file__), 'scratch.db')\n"
                "    )\n"
            )
        self.addCleanup(sys.modules.pop, name, None)
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "004_extra.up.sql",
            "CREATE TABLE extras (id INTEGER);",
        )
        # The scratch database already holds 003_trim, so the rehearsal
        # there runs 004_extra alone while both are pending on the real
        # database. The receipt would cover a drop nothing proved.
        seed = f"scratch_seed_{id(self)}"
        with open(os.path.join(self.dir.name, f"{seed}.py"), "w") as f:
            f.write(CONFIG_TEMPLATE.replace('"cli.db"', '"scratch.db"'))
        self.addCleanup(sys.modules.pop, seed, None)
        with contextlib.redirect_stdout(io.StringIO()):
            main(
                [
                    "migrate",
                    "--unrehearsed",
                    "--target",
                    "003_trim",
                    "--config",
                    seed,
                ]
            )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["rehearse", "--config", name])
        out = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("rehearsed 004_extra", out)
        self.assertIn("receipt not recorded", out)
        self.assertNotIn("sustained_rehearsals", self.table_names())

    def test_a_scratch_rehearsal_records_on_the_real_database(self):
        name = f"scratch_receipt_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(
                CONFIG_TEMPLATE + "\n"
                "def get_rehearsal_connection():\n"
                "    return sqlite3.connect(\n"
                "        os.path.join(os.path.dirname(__file__), 'scratch.db')\n"
                "    )\n"
            )
        self.addCleanup(sys.modules.pop, name, None)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["rehearse", "--config", name])
        self.assertEqual(code, 0)
        self.assertIn("receipt recorded", stdout.getvalue())
        # The receipt landed on the real database, so the gate opens
        # there even though the proving happened elsewhere.
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("applied  003_trim", out)


CALLBACK_CONFIG = """
import os
import sqlite3

def get_connection():
    return sqlite3.connect(os.path.join(os.path.dirname(__file__), "cli.db"))

migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
log_path = os.path.join(os.path.dirname(__file__), "callbacks.log")

def _note(line):
    with open(log_path, "a") as handle:
        handle.write(line + "\\n")

def before_migrate(connection):
    _note("before")

def after_migrate(connection, applied):
    _note("after " + ",".join(applied))

def on_error(connection, migration_id, error):
    _note("error " + str(migration_id))
"""


class MigrateModelsCliTestCase(CliBase):
    """`sustained migrate` closes model drift and reports what it left."""

    def _config(self, extra=""):
        name = f"migrate_models_{id(self)}"
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(
                CONFIG_TEMPLATE + "\nfrom sustained import create_model\n"
                "from sustained.schema import Integer, Text\n"
                "Drifted = create_model('Drifted', 'users')\n"
                "Drifted.tableColumns = {'id': Integer(primary_key=True), "
                "'bio': Text()}\n"
                "Drifted.columns = ('id', 'bio')\n"
                "models = [Drifted]\n" + extra
            )
        self.addCleanup(sys.modules.pop, name, None)
        return name

    def _run(self, name, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([*argv, "--config", name])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_migrate_applies_the_diff_and_verifies_the_schema(self):
        name = self._config()
        code, out, _ = self._run(name, "migrate")
        self.assertEqual(code, 0)
        self.assertIn("applied  001_users", out)
        self.assertIn("applied  auto_", out)
        self.assertIn("schema matches the models", out)
        self.assertIn("users", self.table_names())

    def test_a_second_run_applies_nothing_and_still_verifies(self):
        name = self._config()
        self._run(name, "migrate")
        code, out, _ = self._run(name, "migrate")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to apply.", out)
        self.assertIn("schema matches the models", out)

    def test_the_generated_migration_does_not_break_a_later_command(self):
        name = self._config()
        self._run(name, "migrate")
        code, out, _ = self._run(name, "validate")
        self.assertEqual(code, 0)
        self.assertIn("OK", out)

    def test_a_target_applies_the_registered_migrations_only(self):
        name = self._config()
        code, out, _ = self._run(name, "migrate", "--target", "001_users")
        self.assertEqual(code, 0)
        self.assertNotIn("auto_", out)
        self.assertNotIn("schema matches the models", out)

    def test_drift_the_run_could_not_close_is_reported(self):
        # The verification runs after the migration, so a gap here means
        # the generated statements did not do what the models asked. That
        # is rare enough to stage rather than provoke.
        name = self._config()
        with mock.patch.object(
            Migrator, "drift", return_value=["column 'users.bio' was not added"]
        ):
            code, out, _ = self._run(name, "migrate")
        self.assertEqual(code, 0)
        self.assertIn("drift    column 'users.bio' was not added", out)
        self.assertNotIn("schema matches the models", out)


class CallbackCliTestCase(CliBase):
    """The config module's hooks around the migrate command."""

    def _write_config(self, name, body):
        with open(os.path.join(self.dir.name, f"{name}.py"), "w") as f:
            f.write(body)
        self.addCleanup(sys.modules.pop, name, None)
        return name

    def _run(self, name, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([*argv, "--config", name])
        return code, stdout.getvalue(), stderr.getvalue()

    def _log(self):
        path = os.path.join(self.dir.name, "callbacks.log")
        if not os.path.exists(path):
            return []
        with open(path) as handle:
            return handle.read().split()

    def test_callbacks_fire_around_a_successful_run(self):
        name = self._write_config(f"cb_{id(self)}", CALLBACK_CONFIG)
        code, _, _ = self._run(name, "migrate")
        self.assertEqual(code, 0)
        self.assertEqual(self._log(), ["before", "after", "001_users,002_flag"])

    def test_after_migrate_is_skipped_when_nothing_applied(self):
        name = self._write_config(f"cb_empty_{id(self)}", CALLBACK_CONFIG)
        self._run(name, "migrate")
        os.remove(os.path.join(self.dir.name, "callbacks.log"))
        self._run(name, "migrate")
        self.assertEqual(self._log(), ["before"])

    def test_on_error_names_the_migration_that_failed(self):
        name = self._write_config(f"cb_error_{id(self)}", CALLBACK_CONFIG)
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "003_bad.up.sql",
            "CRATE TABLE oops (id INTEGER);",
        )
        code, _, err = self._run(name, "migrate")
        self.assertEqual(code, 1)
        self.assertEqual(self._log(), ["before", "error", "003_bad"])
        self.assertIn("error in '003_bad': ", err)
        self.assertIn("syntax error", err)

    def test_a_raising_callback_does_not_hide_the_migration_error(self):
        body = CALLBACK_CONFIG.replace(
            'def on_error(connection, migration_id, error):\n    _note("error "'
            " + str(migration_id))",
            "def on_error(connection, migration_id, error):\n"
            '    raise RuntimeError("hook is broken")',
        )
        name = self._write_config(f"cb_raise_{id(self)}", body)
        self._write(
            os.path.join(self.dir.name, "migrations"),
            "003_bad.up.sql",
            "CRATE TABLE oops (id INTEGER);",
        )
        code, _, err = self._run(name, "migrate")
        self.assertEqual(code, 1)
        self.assertIn("on_error raised RuntimeError('hook is broken')", err)
        self.assertIn("syntax error", err)

    def test_a_config_without_callbacks_still_migrates(self):
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("applied  001_users", out)


if __name__ == "__main__":
    unittest.main()
