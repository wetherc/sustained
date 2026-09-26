"""Tests for the static migration scan behind the plan command."""

import unittest

from sustained.analysis import (
    MigrationStatement,
    PendingSummary,
    destructive_statements,
    normalize_statement,
    scannable_forms,
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


class HiddenDropsTestCase(unittest.TestCase):
    """Drops and deletes that do not open their statement."""

    def test_a_drop_after_another_action_is_labelled(self):
        statements = [
            "ALTER TABLE t ADD x int, DROP y",
            "ALTER TABLE t ALTER x TYPE bigint, DROP COLUMN y",
            "ALTER TABLE t ALTER COLUMN x DROP DEFAULT, DROP y",
        ]
        self.assertEqual(destructive_statements(statements), statements)

    def test_a_drop_after_if_exists_or_only_is_labelled(self):
        statements = [
            "ALTER TABLE IF EXISTS t DROP y",
            "ALTER TABLE ONLY t DROP y",
            "ALTER TABLE IF EXISTS ONLY t DROP y",
        ]
        self.assertEqual(destructive_statements(statements), statements)

    def test_other_drops_in_an_action_list_are_not_labelled(self):
        statements = [
            "ALTER TABLE t ADD x int, DROP INDEX i",
            "ALTER TABLE t ALTER COLUMN x DROP DEFAULT",
            "ALTER TABLE t ADD x int, ALTER COLUMN y DROP NOT NULL",
        ]
        self.assertEqual(destructive_statements(statements), [])

    def test_a_delete_without_from_is_labelled(self):
        statements = [
            "DELETE t WHERE id = 1",
            "DELETE t1 FROM t1 JOIN t2 ON t1.id = t2.id",
            "DELETE TOP (10) FROM t",
            "WITH old AS (SELECT id FROM t) DELETE old",
            "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
        ]
        self.assertEqual(destructive_statements(statements), statements)

    def test_a_referential_action_is_not_a_delete(self):
        statement = (
            "ALTER TABLE c ADD CONSTRAINT fk FOREIGN KEY (p) "
            "REFERENCES p (id) ON DELETE CASCADE"
        )
        self.assertEqual(destructive_statements([statement]), [])

    def test_a_drop_after_a_backslash_escape_is_labelled(self):
        # MySQL reads 'it\'s' as one literal, so the DROP is real there.
        statement = "ALTER TABLE t COMMENT = 'it\\'s', DROP y, COMMENT 'z'"
        self.assertEqual(destructive_statements([statement]), [statement])

    def test_a_backslash_ending_a_standard_literal_hides_nothing(self):
        # Postgres reads 'C:\' as one literal, so the DROP is real there.
        statement = "UPDATE t SET p = 'C:\\'; ALTER TABLE t DROP y; SELECT 'x'"
        self.assertEqual(destructive_statements([statement]), [statement])

    def test_only_a_statement_with_a_backslash_is_read_twice(self):
        self.assertEqual(scannable_forms("SELECT 'a'"), ("SELECT ''",))
        self.assertEqual(
            scannable_forms("SELECT 'a\\'' , 'b'"),
            ("SELECT ''b'", "SELECT '' , ''"),
        )


class MarkedStatementTestCase(unittest.TestCase):
    def test_a_marked_statement_is_labelled_whatever_its_text(self):
        statement = MigrationStatement(
            "ALTER TABLE t ALTER COLUMN p TYPE NUMERIC(18, 2)", destructive=True
        )
        self.assertEqual(
            destructive_statements([statement]),
            ["ALTER TABLE t ALTER COLUMN p TYPE NUMERIC(18, 2)"],
        )

    def test_an_unmarked_statement_reads_by_its_text(self):
        statement = MigrationStatement("ALTER TABLE t ALTER COLUMN p TYPE bigint")
        self.assertFalse(statement.destructive)
        self.assertEqual(destructive_statements([statement]), [])

    def test_a_wrapped_statement_keeps_its_mark(self):
        inner = MigrationStatement("ALTER TABLE t ALTER p TYPE int", destructive=True)
        self.assertTrue(MigrationStatement(inner, "002").destructive)
        self.assertFalse(
            MigrationStatement(inner, "002", destructive=False).destructive
        )

    def test_summarize_keeps_the_mark(self):
        statement = MigrationStatement(
            "ALTER TABLE t ALTER p TYPE int", destructive=True
        )
        migration = Migration("auto_1", up=[statement], down=None)
        summary = summarize(migration, "pending")
        self.assertEqual(summary.destructive, ["ALTER TABLE t ALTER p TYPE int"])
        self.assertEqual(summary.sql[0].migration_id, "auto_1")


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


class DollarQuotedTextTestCase(unittest.TestCase):
    """A function body is not scanned; a DO block's body runs, and is."""

    def test_a_drop_inside_a_function_body_is_not_labelled(self):
        statements = [
            "CREATE FUNCTION purge() RETURNS void AS $$ BEGIN DELETE FROM t; "
            "END $$ LANGUAGE plpgsql",
            "CREATE PROCEDURE p() AS $body$ DROP TABLE t $body$ LANGUAGE sql",
            "INSERT INTO audit (note) VALUES ($$DROP TABLE users$$)",
        ]
        self.assertEqual(destructive_statements(statements), [])
        self.assertEqual(
            scannable_statement("SELECT $x$ DROP TABLE t $x$"), "SELECT $$"
        )

    def test_a_drop_after_a_dollar_quote_is_labelled(self):
        statement = "SELECT $$ it's $$; DROP TABLE users"
        self.assertEqual(destructive_statements([statement]), [statement])

    def test_a_drop_inside_a_do_block_is_labelled(self):
        statements = [
            "DO $$ BEGIN DROP TABLE t; END $$",
            "/* cleanup */ do $tag$ begin truncate t; end $tag$",
        ]
        self.assertEqual(
            destructive_statements(statements),
            [
                "DO $$ BEGIN DROP TABLE t; END $$",
                "do $tag$ begin truncate t; end $tag$",
            ],
        )

    def test_a_do_block_reads_its_own_literals_and_comments(self):
        statements = [
            "DO $$ BEGIN RAISE NOTICE 'DROP TABLE t'; END $$",
            "DO $$ BEGIN -- DROP TABLE t\n NULL; END $$",
        ]
        self.assertEqual(destructive_statements(statements), [])

    def test_a_dollar_inside_a_word_opens_no_quote(self):
        statement = "ALTER TABLE a$b DROP COLUMN c$d"
        self.assertEqual(destructive_statements([statement]), [statement])


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

    def test_statements_name_their_migration(self):
        migration = Migration(
            "005_index",
            up="CREATE INDEX i ON t (x)",
            transactional=False,
        )
        statement = summarize(migration, "pending").sql[0]
        self.assertEqual(statement.migration_id, "005_index")
        self.assertFalse(statement.transactional)

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
