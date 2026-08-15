"""Tests for the static migration scan behind the plan command."""

import unittest

from sustained.analysis import PendingSummary, destructive_statements, summarize
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

    def test_keeps_creates_out(self):
        statements = [
            "CREATE TABLE users (id INTEGER)",
            "ALTER TABLE users ADD COLUMN bio TEXT",
            "DELETE FROM users WHERE id = 1",
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


class SummarizeTestCase(unittest.TestCase):
    def test_counts_statements_and_labels_drops(self):
        migration = Migration(
            "003_cleanup",
            up=["CREATE TABLE keep (id INTEGER)", "DROP TABLE legacy"],
            down="DROP TABLE keep",
        )
        self.assertEqual(
            summarize(migration, "pending"),
            PendingSummary("003_cleanup", "pending", False, 2, ["DROP TABLE legacy"]),
        )

    def test_repeatable_carries_its_state(self):
        migration = Migration(
            "vw_active", up="CREATE VIEW v AS SELECT 1", repeatable=True
        )
        summary = summarize(migration, "changed")
        self.assertEqual(summary.state, "changed")
        self.assertTrue(summary.repeatable)
        self.assertEqual(summary.statements, 1)

    def test_callable_step_counts_nothing(self):
        migration = Migration("004_backfill", up=lambda connection: None)
        summary = summarize(migration, "pending")
        self.assertIsNone(summary.statements)
        self.assertEqual(summary.destructive, [])


if __name__ == "__main__":
    unittest.main()
