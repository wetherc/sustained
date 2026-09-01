"""Tests for the static migration scan behind the plan command."""

import unittest

from sustained.analysis import (
    PendingSummary,
    destructive_statements,
    normalize_statement,
    scannable_statement,
    summarize,
)
from sustained.migrations import Migration


class DestructiveStatementsTestCase(unittest.TestCase):
    def test_finds_drop_table(self):
        self.assertEqual(
            destructive_statements(["DROP TABLE users"]), ["DROP TABLE users"]
        )

    def test_finds_drop_column_and_truncate(self):
        statements = [
            "ALTER TABLE users DROP COLUMN legacy",
            "TRUNCATE TABLE sessions",
        ]
        self.assertEqual(destructive_statements(statements), statements)

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(
            destructive_statements(["drop   table  users"]), ["drop table users"]
        )

    def test_finds_bare_column_drop(self):
        self.assertEqual(
            destructive_statements(["ALTER TABLE users DROP legacy"]),
            ["ALTER TABLE users DROP legacy"],
        )

    def test_bare_column_drop_ignores_case_and_spacing(self):
        self.assertEqual(
            destructive_statements(["alter   table users\n  drop  legacy"]),
            ["alter table users drop legacy"],
        )

    def test_constraint_drops_are_labelled(self):
        statements = [
            "ALTER TABLE users DROP CONSTRAINT fk_x",
            "ALTER TABLE users DROP FOREIGN KEY fk_x",
            "ALTER TABLE users DROP CHECK ck_users_status_enum",
        ]
        self.assertEqual(destructive_statements(statements), statements)

    def test_index_and_key_drops_are_not_labelled(self):
        statements = [
            "ALTER TABLE users DROP INDEX idx_users_email",
            "ALTER TABLE users DROP PRIMARY KEY",
            "DROP INDEX idx_users_email",
        ]
        self.assertEqual(destructive_statements(statements), [])

    def test_keeps_creates_out(self):
        statements = [
            "CREATE TABLE users (id INTEGER)",
            "ALTER TABLE users ADD COLUMN bio TEXT",
        ]
        self.assertEqual(destructive_statements(statements), [])

    def test_word_boundaries_hold(self):
        self.assertEqual(destructive_statements(["CREATE TABLE dropped (id INT)"]), [])
        self.assertEqual(
            destructive_statements(["INSERT INTO truncated VALUES (1)"]), []
        )

    def test_ignores_line_comments(self):
        self.assertEqual(
            destructive_statements(["CREATE TABLE users (id INT) -- DROP TABLE old"]),
            [],
        )

    def test_ignores_block_comments(self):
        self.assertEqual(
            destructive_statements(
                ["CREATE TABLE users (id INT) /* DROP TABLE old */"]
            ),
            [],
        )
        self.assertEqual(
            destructive_statements(["CREATE TABLE users (id INT) /* keep\nthis */"]),
            [],
        )

    def test_drops_the_comment_from_a_labelled_statement(self):
        self.assertEqual(
            destructive_statements(["DROP TABLE users -- no longer read"]),
            ["DROP TABLE users"],
        )
        self.assertEqual(
            destructive_statements(["DROP TABLE users /* no longer read */"]),
            ["DROP TABLE users"],
        )

    def test_collapses_whitespace(self):
        self.assertEqual(
            destructive_statements(["ALTER TABLE users\n  DROP COLUMN bio"]),
            ["ALTER TABLE users DROP COLUMN bio"],
        )

    def test_accepts_one_string(self):
        self.assertEqual(
            destructive_statements("DROP TABLE users"), ["DROP TABLE users"]
        )


class RemovesDataTestCase(unittest.TestCase):
    """The statements that remove rows or whole objects."""

    labelled = [
        "DELETE FROM users",
        "DELETE FROM users WHERE id = 1",
        "delete\n  from users",
        "DROP VIEW active_users",
        "DROP VIEW IF EXISTS active_users",
        "DROP MATERIALIZED VIEW user_counts",
        "DROP DATABASE app",
        "DROP SCHEMA reporting CASCADE",
        "DROP SCHEMA IF EXISTS reporting CASCADE",
    ]
    passed = [
        "DROP SCHEMA reporting",
        "DROP SCHEMA reporting RESTRICT",
        "INSERT INTO deleted_users SELECT * FROM users",
        "UPDATE users SET deleted_from = 'x'",
        "CREATE VIEW active_users AS SELECT 1",
        "CREATE MATERIALIZED VIEW user_counts AS SELECT 1",
        "REFRESH MATERIALIZED VIEW user_counts",
        "SELECT deleted, viewed FROM users",
    ]

    def test_labels_every_removing_statement(self):
        for statement in self.labelled:
            with self.subTest(statement=statement):
                self.assertEqual(len(destructive_statements([statement])), 1)

    def test_passes_the_rest(self):
        for statement in self.passed:
            with self.subTest(statement=statement):
                self.assertEqual(destructive_statements([statement]), [])


class QuotedTextTestCase(unittest.TestCase):
    """A scan reads no text inside quotes, and no comment inside quotes."""

    def test_string_literal_holding_a_drop_is_not_labelled(self):
        statements = [
            "INSERT INTO audit (note) VALUES ('DELETE FROM users')",
            "INSERT INTO audit (note) VALUES ('DROP TABLE users')",
            "INSERT INTO audit (note) VALUES ('please TRUNCATE me')",
            'CREATE TABLE "DROP TABLE users" (id INT)',
            "CREATE TABLE `DROP TABLE users` (id INT)",
        ]
        self.assertEqual(destructive_statements(statements), [])

    def test_escaped_quote_does_not_end_the_literal(self):
        statement = "INSERT INTO audit (note) VALUES ('it''s DROP TABLE t')"
        self.assertEqual(destructive_statements([statement]), [])

    def test_a_dash_dash_inside_a_literal_starts_no_comment(self):
        statement = "INSERT INTO audit (note) VALUES ('a -- b') ; DROP TABLE users"
        self.assertEqual(
            destructive_statements([statement]),
            ["INSERT INTO audit (note) VALUES ('a -- b') ; DROP TABLE users"],
        )

    def test_normalize_keeps_the_literal_text(self):
        self.assertEqual(
            normalize_statement("INSERT INTO audit VALUES ('a -- b')"),
            "INSERT INTO audit VALUES ('a -- b')",
        )

    def test_scannable_empties_the_literal(self):
        self.assertEqual(
            scannable_statement("INSERT INTO audit VALUES ('a -- b') -- gone"),
            "INSERT INTO audit VALUES ('')",
        )

    def test_an_unclosed_quote_reads_as_plain_sql(self):
        self.assertEqual(
            destructive_statements(["DROP TABLE users -- it's gone"]),
            ["DROP TABLE users"],
        )
        self.assertEqual(
            destructive_statements(["SELECT 'unclosed, DROP TABLE users"]),
            ["SELECT 'unclosed, DROP TABLE users"],
        )

    def test_a_drop_inside_a_block_comment_is_not_labelled(self):
        self.assertEqual(
            destructive_statements(["SELECT 1 /* DELETE FROM users */"]), []
        )


class SummarizeTestCase(unittest.TestCase):
    def test_carries_statements_and_labels_drops(self):
        migration = Migration(
            "003_cleanup",
            up=["CREATE TABLE keep (id INTEGER)", "DROP TABLE legacy"],
            down="DROP TABLE keep",
        )
        self.assertEqual(
            summarize(migration, "pending"),
            PendingSummary(
                "003_cleanup",
                "pending",
                False,
                ["CREATE TABLE keep (id INTEGER)", "DROP TABLE legacy"],
                ["DROP TABLE legacy"],
            ),
        )

    def test_repeatable_carries_its_state(self):
        migration = Migration(
            "vw_active", up="CREATE VIEW v AS SELECT 1", repeatable=True
        )
        summary = summarize(migration, "changed")
        self.assertEqual(summary.state, "changed")
        self.assertTrue(summary.repeatable)
        self.assertEqual(summary.sql, ["CREATE VIEW v AS SELECT 1"])

    def test_callable_step_counts_nothing(self):
        migration = Migration("004_backfill", up=lambda connection: None)
        summary = summarize(migration, "pending")
        self.assertIsNone(summary.sql)
        self.assertEqual(summary.destructive, [])


if __name__ == "__main__":
    unittest.main()
