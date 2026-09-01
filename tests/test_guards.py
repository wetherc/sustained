import unittest

from sustained.dialects import Dialects
from sustained.exceptions import GuardBlocked
from sustained.guards import (
    BLOCK,
    WARN,
    Verdict,
    blocking,
    index_must_be_concurrent,
    max_statements,
    no_drops,
    no_lock_without_timeout,
    no_table_rewrite,
    run_guards,
    warnings_only,
)


class NoDropsTest(unittest.TestCase):
    def setUp(self):
        self.guard = no_drops()

    def run_on(self, statements, dialect=Dialects.DEFAULT):
        return self.guard(statements, dialect)

    def test_blocks_table_drop(self):
        verdicts = self.run_on(["DROP TABLE users"])
        self.assertEqual(verdicts, [Verdict("no_drops", BLOCK, "DROP TABLE users")])

    def test_blocks_column_drop(self):
        verdicts = self.run_on(["ALTER TABLE users DROP COLUMN bio"])
        self.assertEqual(len(verdicts), 1)

    def test_blocks_mysql_column_drop(self):
        verdicts = self.run_on(["ALTER TABLE users DROP bio"])
        self.assertEqual(len(verdicts), 1)

    def test_blocks_view_and_schema_drops(self):
        verdicts = self.run_on(
            [
                "DROP VIEW v",
                "DROP MATERIALIZED VIEW mv",
                "DROP SCHEMA s",
                "DROP DATABASE d",
            ]
        )
        self.assertEqual([v.rule for v in verdicts], ["no_drops"] * 4)

    def test_passes_a_drop_named_in_a_string_literal(self):
        statements = [
            "INSERT INTO audit (note) VALUES ('DROP TABLE users')",
            "INSERT INTO audit (note) VALUES ('DROP MATERIALIZED VIEW mv')",
        ]
        self.assertEqual(self.run_on(statements), [])

    def test_keeps_the_literal_in_the_verdict(self):
        verdicts = self.run_on(["DROP TABLE users -- see ticket 'ABC'"])
        self.assertEqual(verdicts[0].statement, "DROP TABLE users")
        verdicts = self.run_on(["DELETE FROM audit WHERE note = 'x'; DROP TABLE users"])
        self.assertEqual(
            verdicts[0].statement,
            "DELETE FROM audit WHERE note = 'x'; DROP TABLE users",
        )

    def test_blocks_constraint_drops(self):
        statements = [
            "ALTER TABLE users DROP CONSTRAINT users_email_key",
            "ALTER TABLE users DROP CHECK ck_users_status_enum",
            "ALTER TABLE users DROP FOREIGN KEY fk_users_org",
        ]
        verdicts = self.run_on(statements)
        self.assertEqual([v.rule for v in verdicts], ["no_drops"] * 3)

    def test_passes_index_and_key_drops(self):
        statements = [
            "DROP INDEX users_email_idx",
            "ALTER TABLE users DROP KEY k",
        ]
        self.assertEqual(self.run_on(statements), [])

    def test_ignores_commented_drop(self):
        self.assertEqual(self.run_on(["-- DROP TABLE users\nSELECT 1"]), [])

    def test_collapses_whitespace_in_the_verdict(self):
        verdicts = self.run_on(["DROP   TABLE\n  users"])
        self.assertEqual(verdicts[0].statement, "DROP TABLE users")

    def test_truncate_is_not_a_drop(self):
        self.assertEqual(self.run_on(["TRUNCATE TABLE users"]), [])


class IndexMustBeConcurrentTest(unittest.TestCase):
    def setUp(self):
        self.guard = index_must_be_concurrent()

    def test_blocks_plain_index_on_postgres(self):
        verdicts = self.guard(["CREATE INDEX i ON users (email)"], Dialects.POSTGRES)
        self.assertEqual(verdicts[0].rule, "index_must_be_concurrent")
        self.assertEqual(verdicts[0].verdict, BLOCK)

    def test_passes_concurrent_index(self):
        verdicts = self.guard(
            ["CREATE INDEX CONCURRENTLY i ON users (email)"], Dialects.POSTGRES
        )
        self.assertEqual(verdicts, [])

    def test_passes_unique_concurrent_index(self):
        verdicts = self.guard(
            ["CREATE UNIQUE INDEX CONCURRENTLY i ON users (email)"], Dialects.POSTGRES
        )
        self.assertEqual(verdicts, [])

    def test_blocks_unique_plain_index(self):
        verdicts = self.guard(
            ["CREATE UNIQUE INDEX i ON users (email)"], Dialects.POSTGRES
        )
        self.assertEqual(len(verdicts), 1)

    def test_silent_off_postgres(self):
        verdicts = self.guard(["CREATE INDEX i ON users (email)"], Dialects.DEFAULT)
        self.assertEqual(verdicts, [])

    def test_concurrently_inside_a_literal_does_not_count(self):
        verdicts = self.guard(
            ["CREATE INDEX i ON users (note) WHERE note = 'CONCURRENTLY'"],
            Dialects.POSTGRES,
        )
        self.assertEqual(len(verdicts), 1)


class NoTableRewriteTest(unittest.TestCase):
    def setUp(self):
        self.guard = no_table_rewrite()

    def run_on(self, statements):
        return self.guard(statements, Dialects.POSTGRES)

    def test_warns_on_type_change(self):
        verdicts = self.run_on(["ALTER TABLE users ALTER COLUMN age TYPE BIGINT"])
        self.assertEqual(verdicts[0].verdict, WARN)
        self.assertEqual(verdicts[0].rule, "no_table_rewrite")

    def test_warns_on_set_data_type(self):
        verdicts = self.run_on(
            ["ALTER TABLE users ALTER COLUMN age SET DATA TYPE BIGINT"]
        )
        self.assertEqual(len(verdicts), 1)

    def test_warns_on_mysql_modify(self):
        verdicts = self.run_on(["ALTER TABLE users MODIFY COLUMN age BIGINT"])
        self.assertEqual(len(verdicts), 1)

    def test_warns_on_set_not_null(self):
        verdicts = self.run_on(["ALTER TABLE users ALTER COLUMN bio SET NOT NULL"])
        self.assertEqual(len(verdicts), 1)

    def test_warns_on_not_null_column_without_default(self):
        verdicts = self.run_on(["ALTER TABLE users ADD COLUMN bio TEXT NOT NULL"])
        self.assertEqual(len(verdicts), 1)

    def test_passes_not_null_column_with_default(self):
        verdicts = self.run_on(
            ["ALTER TABLE users ADD COLUMN bio TEXT NOT NULL DEFAULT ''"]
        )
        self.assertEqual(verdicts, [])

    def test_default_inside_a_comment_does_not_count(self):
        verdicts = self.run_on(
            ["ALTER TABLE users ADD COLUMN bio TEXT NOT NULL /* DEFAULT '' */"]
        )
        self.assertEqual(len(verdicts), 1)

    def test_passes_a_plain_add(self):
        self.assertEqual(self.run_on(["ALTER TABLE users ADD COLUMN bio TEXT"]), [])


class NoLockWithoutTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.guard = no_lock_without_timeout()

    def run_on(self, statements):
        return self.guard(statements, Dialects.POSTGRES)

    def test_blocks_alter_without_a_timeout(self):
        verdicts = self.run_on(["ALTER TABLE users ADD COLUMN bio TEXT"])
        self.assertEqual(verdicts[0].rule, "no_lock_without_timeout")
        self.assertEqual(verdicts[0].verdict, BLOCK)

    def test_blocks_table_drop_without_a_timeout(self):
        self.assertEqual(len(self.run_on(["DROP TABLE users"])), 1)

    def test_one_timeout_covers_the_run(self):
        statements = [
            "SET lock_timeout = '5s'",
            "ALTER TABLE users ADD COLUMN bio TEXT",
            "ALTER TABLE shows ADD COLUMN slug TEXT",
        ]
        self.assertEqual(self.run_on(statements), [])

    def test_local_timeout_counts(self):
        statements = ["SET LOCAL lock_timeout = '5s'", "DROP TABLE users"]
        self.assertEqual(self.run_on(statements), [])

    def test_passes_a_run_that_takes_no_table_lock(self):
        self.assertEqual(self.run_on(["CREATE TABLE users (id INTEGER)"]), [])

    def test_session_timeout_counts(self):
        statements = ["SET SESSION lock_timeout = '5s'", "DROP TABLE users"]
        self.assertEqual(self.run_on(statements), [])

    def test_a_column_named_lock_timeout_is_not_a_timeout(self):
        statements = ["UPDATE settings SET lock_timeout = 5", "DROP TABLE users"]
        verdicts = self.run_on(statements)
        self.assertEqual([v.statement for v in verdicts], ["DROP TABLE users"])

    def test_a_timeout_on_another_setting_does_not_count(self):
        statements = ["SET statement_timeout = '5s'", "DROP TABLE users"]
        self.assertEqual(len(self.run_on(statements)), 1)

    def test_silent_off_postgres(self):
        verdicts = self.guard(["DROP TABLE users"], Dialects.DEFAULT)
        self.assertEqual(verdicts, [])


class MaxStatementsTest(unittest.TestCase):
    def test_blocks_every_statement_past_the_limit(self):
        guard = max_statements(2)
        verdicts = guard(
            ["SELECT 1", "SELECT 2", "SELECT 3", "SELECT 4"], Dialects.DEFAULT
        )
        self.assertEqual([v.statement for v in verdicts], ["SELECT 3", "SELECT 4"])
        self.assertEqual(verdicts[0].rule, "max_statements(2)")

    def test_passes_a_run_at_the_limit(self):
        guard = max_statements(2)
        self.assertEqual(guard(["SELECT 1", "SELECT 2"], Dialects.DEFAULT), [])

    def test_refuses_a_limit_below_one(self):
        with self.assertRaises(ValueError):
            max_statements(0)


class RunGuardsTest(unittest.TestCase):
    def test_runs_every_guard_in_order(self):
        verdicts = run_guards(
            [no_drops(), max_statements(1)],
            ["DROP TABLE users", "DROP TABLE shows"],
            Dialects.DEFAULT,
        )
        self.assertEqual(
            [v.rule for v in verdicts],
            ["no_drops", "no_drops", "max_statements(1)"],
        )

    def test_no_guards_no_verdicts(self):
        self.assertEqual(run_guards([], ["DROP TABLE users"], Dialects.DEFAULT), [])

    def test_splits_blocking_from_warning(self):
        verdicts = run_guards(
            [no_drops(), no_table_rewrite()],
            ["DROP TABLE users", "ALTER TABLE users ALTER COLUMN age TYPE BIGINT"],
            Dialects.DEFAULT,
        )
        self.assertEqual([v.rule for v in blocking(verdicts)], ["no_drops"])
        self.assertEqual(
            [v.rule for v in warnings_only(verdicts)], ["no_table_rewrite"]
        )


class GuardBlockedTest(unittest.TestCase):
    def test_message_names_the_rule_and_the_statement(self):
        error = GuardBlocked([Verdict("no_drops", BLOCK, "DROP TABLE users")])
        self.assertIn("no_drops  DROP TABLE users", str(error))
        self.assertIn("A guard blocked this run", str(error))
        self.assertEqual(len(error.verdicts), 1)
