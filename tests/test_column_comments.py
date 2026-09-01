"""
Tests for column comments: per-dialect rendering in CREATE TABLE and ADD
COLUMN, the set_column_comment ddl step with its derived down step, the
dialects that store no comments, and the checksum stability of columns
that carry none.
"""

import json
import unittest

import sustained.autogenerate as autogenerate_module
from sustained import DialectError, Model
from sustained.autogenerate import autogenerate, diff_schema
from sustained.ddl import add_column, create_table, set_column_comment
from sustained.dialects import Dialects
from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedTable,
    Snapshot,
    introspect_schema,
)
from sustained.schema import ColumnDef, Integer, String

ANSI = Dialects.get_compiler(Dialects.DEFAULT)
POSTGRES = Dialects.get_compiler(Dialects.POSTGRES)
MYSQL = Dialects.get_compiler(Dialects.MYSQL)
MSSQL = Dialects.get_compiler(Dialects.MSSQL)
PRESTO = Dialects.get_compiler(Dialects.PRESTO)
ATHENA = Dialects.get_compiler(Dialects.ATHENA)
DUCKDB = Dialects.get_compiler(Dialects.DUCKDB)


class Account(Model):
    tableName = "accounts"
    tableColumns = {
        "id": Integer(primary_key=True),
        "email": String(120, nullable=False, comment="Login address"),
        "notes": String(255),
    }


class TestColumnDefValidation(unittest.TestCase):
    def test_empty_comment_raises(self):
        with self.assertRaisesRegex(ValueError, "non-empty"):
            Integer(comment="")

    def test_whitespace_comment_raises(self):
        with self.assertRaisesRegex(ValueError, "non-empty"):
            Integer(comment="   ")

    def test_non_string_comment_raises(self):
        with self.assertRaisesRegex(ValueError, "non-empty"):
            Integer(comment=7)


class TestCreateTableRendering(unittest.TestCase):
    def test_postgres_appends_comment_statements(self):
        statements = create_table(Account).render(POSTGRES)
        self.assertEqual(len(statements), 2)
        self.assertNotIn("COMMENT", statements[0])
        self.assertEqual(
            statements[1],
            'COMMENT ON COLUMN "accounts"."email" IS \'Login address\'',
        )

    def test_duckdb_appends_comment_statements(self):
        statements = create_table(Account).render(DUCKDB)
        self.assertEqual(
            statements[1],
            'COMMENT ON COLUMN "accounts"."email" IS \'Login address\'',
        )

    def test_mysql_renders_comment_inline(self):
        statements = create_table(Account).render(MYSQL)
        self.assertEqual(len(statements), 1)
        self.assertIn(
            "`email` VARCHAR(120) NOT NULL COMMENT 'Login address'", statements[0]
        )

    def test_presto_renders_comment_inline(self):
        statements = create_table(Account).render(PRESTO)
        self.assertEqual(len(statements), 1)
        self.assertIn("COMMENT 'Login address'", statements[0])

    def test_ansi_renders_no_comment(self):
        statements = create_table(Account).render(ANSI)
        self.assertEqual(len(statements), 1)
        self.assertNotIn("COMMENT", statements[0])

    def test_mssql_renders_no_comment(self):
        statements = create_table(Account).render(MSSQL)
        self.assertEqual(len(statements), 1)
        self.assertNotIn("COMMENT", statements[0])

    def test_comment_quotes_escape(self):
        step = create_table(
            "notes", columns={"body": String(comment="the user's words")}
        )
        rendered = step.render(POSTGRES)
        self.assertIn("IS 'the user''s words'", rendered[1])

    def test_model_statements_carry_comments(self):
        class PostgresAccount(Account):
            tableName = "accounts"

        PostgresAccount.set_dialect(Dialects.POSTGRES)
        statements = PostgresAccount.create_table_statements()
        self.assertIn(
            'COMMENT ON COLUMN "accounts"."email" IS \'Login address\'',
            statements,
        )


class TestAddColumnRendering(unittest.TestCase):
    def test_postgres_add_column_appends_comment(self):
        step = add_column("accounts", "tier", String(20, comment="Billing tier"))
        statements = step.render(POSTGRES)
        self.assertEqual(len(statements), 2)
        self.assertEqual(
            statements[1],
            'COMMENT ON COLUMN "accounts"."tier" IS \'Billing tier\'',
        )

    def test_mysql_add_column_inlines_comment(self):
        step = add_column("accounts", "tier", String(20, comment="Billing tier"))
        statements = step.render(MYSQL)
        self.assertEqual(len(statements), 1)
        self.assertIn("COMMENT 'Billing tier'", statements[0])

    def test_ansi_add_column_drops_comment(self):
        step = add_column("accounts", "tier", String(20, comment="Billing tier"))
        statements = step.render(ANSI)
        self.assertEqual(len(statements), 1)
        self.assertNotIn("COMMENT", statements[0])


class TestSetColumnComment(unittest.TestCase):
    def test_postgres_set(self):
        step = set_column_comment("accounts", "email", "Login address")
        self.assertEqual(
            step.render(POSTGRES),
            ['COMMENT ON COLUMN "accounts"."email" IS \'Login address\''],
        )

    def test_postgres_clear(self):
        step = set_column_comment("accounts", "email", None, previous="Login address")
        self.assertEqual(
            step.render(POSTGRES),
            ['COMMENT ON COLUMN "accounts"."email" IS NULL'],
        )

    def test_presto_set(self):
        step = set_column_comment("accounts", "email", "Login address")
        self.assertEqual(
            step.render(PRESTO),
            ['COMMENT ON COLUMN "accounts"."email" IS \'Login address\''],
        )

    def test_mysql_needs_the_column(self):
        step = set_column_comment("accounts", "email", "Login address")
        with self.assertRaisesRegex(DialectError, "column="):
            step.render(MYSQL)

    def test_mysql_restates_the_column(self):
        step = set_column_comment(
            "accounts",
            "email",
            "Login address",
            column=String(120, nullable=False),
        )
        self.assertEqual(
            step.render(MYSQL),
            [
                "ALTER TABLE `accounts` MODIFY COLUMN `email` VARCHAR(120) "
                "NOT NULL COMMENT 'Login address'"
            ],
        )

    def test_mysql_clear_omits_the_clause(self):
        step = set_column_comment(
            "accounts",
            "email",
            None,
            previous="Login address",
            column=String(120, nullable=False),
        )
        self.assertEqual(
            step.render(MYSQL),
            ["ALTER TABLE `accounts` MODIFY COLUMN `email` VARCHAR(120) NOT NULL"],
        )

    def test_mysql_restatement_keeps_the_default(self):
        step = set_column_comment(
            "accounts",
            "tier",
            "Billing tier",
            column=String(20, default="free"),
        )
        self.assertIn("DEFAULT 'free'", step.render(MYSQL)[0])

    def test_mysql_restatement_never_restates_unique(self):
        step = set_column_comment(
            "accounts",
            "email",
            "Login address",
            column=String(120, unique=True),
        )
        self.assertNotIn("UNIQUE", step.render(MYSQL)[0])

    def test_ansi_raises(self):
        step = set_column_comment("accounts", "email", "Login address")
        with self.assertRaisesRegex(DialectError, "stores no column comments"):
            step.render(ANSI)

    def test_mssql_raises(self):
        step = set_column_comment("accounts", "email", "Login address")
        with self.assertRaisesRegex(DialectError, "stores no column comments"):
            step.render(MSSQL)

    def test_athena_raises(self):
        step = set_column_comment("accounts", "email", "Login address")
        with self.assertRaisesRegex(DialectError, "in place"):
            step.render(ATHENA)

    def test_needs_a_column_name(self):
        with self.assertRaisesRegex(ValueError, "column name"):
            set_column_comment("accounts", "", "x")

    def test_reversible_and_inverse_swaps(self):
        step = set_column_comment(
            "accounts", "email", "Login address", previous="Old words"
        )
        self.assertTrue(step.reversible)
        inverse = step.inverse()
        self.assertEqual(inverse.args["comment"], "Old words")
        self.assertEqual(inverse.args["previous"], "Login address")
        self.assertEqual(
            inverse.render(POSTGRES),
            ['COMMENT ON COLUMN "accounts"."email" IS \'Old words\''],
        )

    def test_inverse_of_a_first_comment_clears(self):
        step = set_column_comment("accounts", "email", "Login address")
        self.assertEqual(
            step.inverse().render(POSTGRES),
            ['COMMENT ON COLUMN "accounts"."email" IS NULL'],
        )


class TestChecksumStability(unittest.TestCase):
    def test_column_without_comment_serializes_as_before(self):
        # The canonical form below is what releases before column
        # comments produced. A column with no comment must keep it, or
        # every applied migration would fail validation on upgrade.
        step = add_column("accounts", "tier", ColumnDef("VARCHAR", length=20))
        column = json.loads(step.signature())["args"]["column"]
        self.assertEqual(
            column,
            {
                "$column": {
                    "type_name": "VARCHAR",
                    "length": 20,
                    "precision": None,
                    "scale": None,
                    "primary_key": False,
                    "nullable": True,
                    "unique": False,
                    "default": None,
                    "references": None,
                    "autoincrement": False,
                    "backfill": None,
                    "enum_name": None,
                    "enum_values": None,
                }
            },
        )

    def test_column_with_comment_changes_the_signature(self):
        bare = add_column("accounts", "tier", String(20)).signature()
        commented = add_column(
            "accounts", "tier", String(20, comment="Billing tier")
        ).signature()
        self.assertNotEqual(bare, commented)

    def test_set_column_comment_signature_is_dialect_independent(self):
        step = set_column_comment("accounts", "email", "Login address")
        parsed = json.loads(step.signature())
        self.assertEqual(parsed["op"], "set_column_comment")
        self.assertEqual(parsed["args"]["comment"], "Login address")


class FakeCursor:
    """Serves canned catalog rows, keyed by a substring of the SQL."""

    def __init__(self, responses):
        self.responses = responses
        self.statements = []
        self._current = []

    def execute(self, sql, params=()):
        self.statements.append(" ".join(sql.split()))
        for fragment, rows in self.responses.items():
            if fragment in sql:
                if rows is None:
                    raise RuntimeError(f"no {fragment} here")
                self._current = rows
                return
        self._current = []

    def fetchall(self):
        return list(self._current)


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class TestPrestoCommentRead(unittest.TestCase):
    """
    Presto and Trino put the comment straight on information_schema.columns,
    so the shared read selects it beside the other column data and retries
    without it when the engine holds no such column.
    """

    WITH_COMMENT = "c.column_default, c.comment"
    WITHOUT_COMMENT = "information_schema.columns"

    def read(self, responses, dialect=Dialects.PRESTO):
        self.cursor = FakeCursor(responses)
        return introspect_schema(FakeConnection(self.cursor), dialect)

    def test_a_column_comment_is_read(self):
        schema = self.read(
            {
                self.WITH_COMMENT: [
                    ("users", "email", "varchar(120)", "NO", None, "Login address")
                ],
            }
        )
        self.assertEqual(schema["users"].columns["email"].comment, "Login address")
        self.assertTrue(schema.comments_read)

    def test_the_columns_are_read_once(self):
        self.read(
            {
                self.WITH_COMMENT: [
                    ("users", "email", "varchar(120)", "NO", None, "Login address")
                ],
            }
        )
        reads = [s for s in self.cursor.statements if "information_schema.columns" in s]
        self.assertEqual(len(reads), 1)

    def test_athena_reads_the_same_way(self):
        schema = self.read(
            {
                self.WITH_COMMENT: [
                    ("users", "email", "varchar(120)", "NO", None, "Login address")
                ],
            },
            dialect=Dialects.ATHENA,
        )
        self.assertEqual(schema["users"].columns["email"].comment, "Login address")

    def test_a_failed_comment_read_degrades(self):
        schema = self.read(
            {
                self.WITH_COMMENT: None,
                self.WITHOUT_COMMENT: [("users", "email", "varchar(120)", "NO", None)],
            }
        )
        self.assertIsNone(schema["users"].columns["email"].comment)
        self.assertFalse(schema.comments_read)

    def test_mssql_asks_for_no_comments(self):
        cursor = FakeCursor(
            {"information_schema.columns": [("users", "id", "int", "NO", None)]}
        )
        schema = introspect_schema(FakeConnection(cursor), Dialects.MSSQL)
        self.assertFalse(schema.comments_read)
        self.assertFalse(any("comment" in s.lower() for s in cursor.statements))


class TestDuckdbCommentRead(unittest.TestCase):
    def setUp(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("the duckdb driver is missing")
        self.connection = duckdb.connect(":memory:")

    def tearDown(self):
        self.connection.close()

    def test_a_comment_is_read_back(self):
        self.connection.execute("CREATE TABLE t (a INT, b INT)")
        self.connection.execute("COMMENT ON COLUMN t.a IS 'the a column'")
        schema = introspect_schema(self.connection, Dialects.DUCKDB)
        self.assertEqual(schema["t"].columns["a"].comment, "the a column")
        self.assertIsNone(schema["t"].columns["b"].comment)
        self.assertTrue(schema.comments_read)


class TestSqliteCommentRead(unittest.TestCase):
    def test_sqlite_leaves_comments_unread(self):
        import sqlite3

        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE t (a INT)")
        schema = introspect_schema(connection, Dialects.DEFAULT)
        self.assertFalse(schema.comments_read)
        self.assertIsNone(schema["t"].columns["a"].comment)


def make_account(class_name, dialect):
    """A model matching Account, bound to one dialect for a drift test."""
    model = type(
        class_name,
        (Model,),
        {
            "tableName": "accounts",
            "tableColumns": {
                "id": Integer(primary_key=True),
                "email": String(120, nullable=False, comment="Login address"),
                "notes": String(255),
            },
        },
    )
    model.set_dialect(dialect)
    return model


class TestCommentDrift(unittest.TestCase):
    """
    diff_schema and autogenerate on a drifted comment, with the catalog
    read stubbed out so each dialect's emission is tested on its own.
    """

    def stub_snapshot(self, snapshot):
        original = autogenerate_module.introspect_schema
        autogenerate_module.introspect_schema = lambda connection, dialect: snapshot
        self.addCleanup(setattr, autogenerate_module, "introspect_schema", original)

    def snapshot(self, model, dialect, email_comment, comments_read=True):
        compiler = Dialects.get_compiler(dialect)
        columns = {
            name: IntrospectedColumn(
                compiler.compile_column_type(coldef),
                coldef.nullable,
                coldef.primary_key,
            )
            for name, coldef in model.tableColumns.items()
        }
        columns["email"] = columns["email"]._replace(comment=email_comment)
        table = IntrospectedTable(columns=columns, primary_key=("id",))
        return Snapshot({"accounts": table}, comments_read=comments_read)

    def diff(self, dialect, email_comment, comments_read=True):
        model = make_account(f"Drift{dialect.name.title()}", dialect)
        self.stub_snapshot(self.snapshot(model, dialect, email_comment, comments_read))
        return model, diff_schema(None, [model], dialect)

    def test_a_drifted_comment_is_reported(self):
        _, diff = self.diff(Dialects.POSTGRES, "Old text")
        self.assertEqual(
            diff.changed_comments,
            [("accounts", "email", "Old text", "Login address")],
        )
        self.assertFalse(diff.is_empty())

    def test_a_matching_comment_diffs_nothing(self):
        _, diff = self.diff(Dialects.POSTGRES, "Login address")
        self.assertEqual(diff.changed_comments, [])

    def test_an_unread_catalog_diffs_no_comments(self):
        _, diff = self.diff(Dialects.POSTGRES, None, comments_read=False)
        self.assertEqual(diff.changed_comments, [])

    def test_an_empty_string_reads_as_no_comment(self):
        # MySQL reports an uncommented column as ''. The declared comment
        # is still missing, and the diff reports the live side as none.
        _, diff = self.diff(Dialects.MYSQL, "")
        self.assertEqual(
            diff.changed_comments,
            [("accounts", "email", None, "Login address")],
        )

    def test_outstanding_and_summary_name_the_drift(self):
        _, diff = self.diff(Dialects.POSTGRES, "Old text")
        self.assertIn(
            "column 'accounts.email' comment is 'Old text', "
            "the models declare 'Login address'",
            diff.outstanding(),
        )
        self.assertIn("set the comment on accounts.email", diff.summary())

    def test_autogenerate_sets_the_comment(self):
        model = make_account("DriftPgGen", Dialects.POSTGRES)
        self.stub_snapshot(self.snapshot(model, Dialects.POSTGRES, "Old text"))
        migration = autogenerate(None, [model], id="c1", dialect=Dialects.POSTGRES)
        self.assertEqual(
            migration.up,
            ['COMMENT ON COLUMN "accounts"."email" IS \'Login address\''],
        )
        self.assertEqual(
            migration.down,
            ['COMMENT ON COLUMN "accounts"."email" IS \'Old text\''],
        )

    def test_autogenerate_clears_an_undeclared_comment(self):
        model = make_account("DriftPgClear", Dialects.POSTGRES)
        snapshot = self.snapshot(model, Dialects.POSTGRES, "Login address")
        snapshot["accounts"].columns["notes"] = (
            snapshot["accounts"].columns["notes"]._replace(comment="stale")
        )
        self.stub_snapshot(snapshot)
        migration = autogenerate(None, [model], id="c2", dialect=Dialects.POSTGRES)
        self.assertEqual(migration.up, ['COMMENT ON COLUMN "accounts"."notes" IS NULL'])
        self.assertEqual(
            migration.down, ['COMMENT ON COLUMN "accounts"."notes" IS \'stale\'']
        )

    def test_mysql_restates_the_column(self):
        model = make_account("DriftMyGen", Dialects.MYSQL)
        self.stub_snapshot(self.snapshot(model, Dialects.MYSQL, "Old text"))
        migration = autogenerate(None, [model], id="c3", dialect=Dialects.MYSQL)
        self.assertEqual(
            migration.up,
            [
                "ALTER TABLE `accounts` MODIFY COLUMN `email` VARCHAR(120) "
                "NOT NULL COMMENT 'Login address'"
            ],
        )
        self.assertEqual(
            migration.down,
            [
                "ALTER TABLE `accounts` MODIFY COLUMN `email` VARCHAR(120) "
                "NOT NULL COMMENT 'Old text'"
            ],
        )


class TestDuckdbCommentDrift(unittest.TestCase):
    """The whole loop against a real DuckDB: drift, migrate, converge."""

    def setUp(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("the duckdb driver is missing")
        self.connection = duckdb.connect(":memory:")
        self.model = make_account("DriftDuck", Dialects.DUCKDB)
        for statement in self.model.create_table_statements():
            self.connection.execute(statement)

    def tearDown(self):
        self.connection.close()

    def test_a_drifted_comment_migrates_back(self):
        self.connection.execute("COMMENT ON COLUMN accounts.email IS 'Old text'")
        diff = diff_schema(self.connection, [self.model], Dialects.DUCKDB)
        self.assertEqual(
            diff.changed_comments,
            [("accounts", "email", "Old text", "Login address")],
        )
        migration = autogenerate(
            self.connection, [self.model], id="d1", dialect=Dialects.DUCKDB
        )
        for statement in migration.up:
            self.connection.execute(statement)
        after = diff_schema(self.connection, [self.model], Dialects.DUCKDB)
        self.assertEqual(after.changed_comments, [])


if __name__ == "__main__":
    unittest.main()
