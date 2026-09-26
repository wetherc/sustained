"""
Tests for the Intent the diff and ddl steps attach to generated SQL.

The sweep test runs every test module that drives the diff, with the
diff's Migration wrapped so each up statement it generates is checked.
Every diff path those modules reach is then covered without repeating
their setups here.
"""

import importlib
import io
import unittest
from unittest import mock

from sustained import autogenerate as autogenerate_module
from sustained import ddl
from sustained.analysis import MigrationStatement, summarize, with_intent
from sustained.dialects import Dialects
from sustained.impact.model import INTENT_KINDS, Intent
from sustained.migrations import Migration, migration_checksum
from sustained.schema import Check, ColumnDef, ForeignKey, Index

DIFF_TEST_MODULES = [
    "tests.test_alter_column_lifts",
    "tests.test_athena",
    "tests.test_autogenerate",
    "tests.test_column_comments",
    "tests.test_constraint_autogenerate",
    "tests.test_duckdb_constraints",
    "tests.test_duckdb_dialect",
    "tests.test_enum_checks",
    "tests.test_enum_columns",
    "tests.test_extra_table_drops",
    "tests.test_foreign_key_schemas",
    "tests.test_index_introspection",
    "tests.test_keyword_identifiers",
    "tests.test_migration_guards",
    "tests.test_mssql_introspection",
    "tests.test_mysql_introspection",
    "tests.test_name_spelling",
    "tests.test_postgres_introspection",
    "tests.test_rename_foreign_keys",
    "tests.test_schema_qualified_drops",
    "tests.test_sqlite_rebuild",
    "tests.test_unique_removal",
]


class WithIntentTestCase(unittest.TestCase):
    def test_attaches_an_intent(self):
        statement = with_intent("DROP INDEX ix", "drop_index", "t", name="ix")
        self.assertIsInstance(statement, MigrationStatement)
        self.assertEqual(statement, "DROP INDEX ix")
        self.assertEqual(
            statement.intent, Intent("drop_index", "t", None, {"name": "ix"})
        )
        self.assertEqual(statement.intent.get("name"), "ix")
        self.assertIsNone(statement.intent.get("missing"))

    def test_keeps_what_the_wrapped_statement_carries(self):
        marked = MigrationStatement("ALTER ...", "m1", False, destructive=True)
        statement = with_intent(marked, "alter_column_type", "t", "c")
        self.assertEqual(statement.migration_id, "m1")
        self.assertFalse(statement.transactional)
        self.assertTrue(statement.destructive)

    def test_refuses_an_unknown_kind(self):
        with self.assertRaises(ValueError):
            with_intent("SELECT 1", "select", None)

    def test_a_rewrapped_statement_keeps_its_intent(self):
        statement = with_intent("DROP TABLE t", "drop_table", "t")
        wrapped = MigrationStatement(statement, "m1", True)
        self.assertEqual(wrapped.intent, statement.intent)
        other = Intent("drop_index", "t")
        self.assertIs(MigrationStatement(statement, intent=other).intent, other)

    def test_intent_takes_no_part_in_equality(self):
        self.assertEqual(
            with_intent("DROP TABLE t", "drop_table", "t"),
            MigrationStatement("DROP TABLE t"),
        )

    def test_intent_takes_no_part_in_the_checksum(self):
        plain = Migration("m1", up=["DROP TABLE t"], down=None)
        tagged = Migration(
            "m1", up=[with_intent("DROP TABLE t", "drop_table", "t")], down=None
        )
        self.assertEqual(migration_checksum(plain), migration_checksum(tagged))

    def test_summarize_keeps_the_intent_of_generated_statements(self):
        migration = Migration(
            "m1", up=[with_intent("DROP TABLE t", "drop_table", "t")], down=None
        )
        summary = summarize(migration, "pending")
        self.assertEqual(summary.sql[0].intent.kind, "drop_table")
        self.assertEqual(summary.sql[0].migration_id, "m1")


class DdlStepIntentTestCase(unittest.TestCase):
    def render(self, step, dialect=Dialects.POSTGRES):
        return step.render(Dialects.get_compiler(dialect))

    def intents(self, step, dialect=Dialects.POSTGRES):
        return [
            (s.intent.kind, s.intent.table, s.intent.column)
            for s in self.render(step, dialect)
        ]

    def test_every_rendered_statement_carries_an_intent(self):
        column = ColumnDef("VARCHAR", length=10, comment="note")
        steps = [
            ddl.create_table(
                "app.users",
                {"id": ColumnDef("INTEGER", primary_key=True), "c": column},
                indexes=[Index("ix_users_c", "c")],
            ),
            ddl.drop_table("users"),
            ddl.add_column("users", "c", column),
            ddl.drop_column("users", "c"),
            ddl.rename_column("users", "a", "b"),
            ddl.rename_table("users", "people"),
            ddl.set_column_comment("users", "c", "x"),
            ddl.add_foreign_key("users", ForeignKey("fk_u", "org_id", "orgs.id")),
            ddl.drop_foreign_key("users", "fk_u"),
            ddl.add_check("users", Check("ck_u", "id > 0")),
            ddl.drop_constraint("users", "ck_u"),
            ddl.create_index("users", Index("ix_u", "id", unique=True)),
            ddl.drop_index("users", "ix_u"),
            ddl.create_enum("mood", "happy"),
            ddl.drop_enum("mood"),
            ddl.add_enum_value("mood", "sad"),
        ]
        for step in steps:
            with self.subTest(step=step.op):
                for statement in self.render(step):
                    self.assertIsNotNone(statement.intent, statement)
                    self.assertIn(statement.intent.kind, INTENT_KINDS)

    def test_create_table_tags_each_statement_it_renders(self):
        step = ddl.create_table(
            "app.users",
            {
                "id": ColumnDef("INTEGER", primary_key=True),
                "mood": ColumnDef(
                    "ENUM", enum_name="mood", enum_values=["a"], comment="m"
                ),
            },
            indexes=[Index("ix_users_id", "id")],
        )
        self.assertEqual(
            [kind for kind, _, _ in self.intents(step)],
            ["create_enum_type", "create_table", "set_column_comment", "create_index"],
        )
        index = self.render(step)[-1].intent
        self.assertEqual(index.table, "app.users")
        self.assertEqual(index.details["columns"], ("id",))
        self.assertFalse(index.details["unique"])

    def test_add_column_says_whether_the_column_is_nullable_and_has_a_default(self):
        step = ddl.add_column(
            "users", "n", ColumnDef("INTEGER", nullable=False, default=0)
        )
        (statement,) = self.render(step)
        self.assertEqual(statement.intent.column, "n")
        self.assertEqual(
            dict(statement.intent.details), {"nullable": False, "has_default": True}
        )

    def test_an_enum_column_on_a_check_dialect_tags_its_check(self):
        step = ddl.add_column(
            "users", "mood", ColumnDef("ENUM", enum_name="mood", enum_values=["a"])
        )
        self.assertEqual(
            [kind for kind, _, _ in self.intents(step, Dialects.MSSQL)],
            ["add_column", "add_check"],
        )
        drop = ddl.add_column(
            "users", "mood", ColumnDef("ENUM", enum_name="mood", enum_values=["a"])
        ).inverse()
        self.assertEqual(
            [kind for kind, _, _ in self.intents(drop, Dialects.MSSQL)],
            ["drop_constraint", "drop_column"],
        )

    def test_rename_and_foreign_key_details(self):
        (rename,) = self.render(ddl.rename_table("users", "people"))
        self.assertEqual(rename.intent.table, "users")
        self.assertEqual(rename.intent.get("new"), "people")
        (fk,) = self.render(
            ddl.add_foreign_key("users", ForeignKey("fk_u", "org_id", "orgs.id"))
        )
        self.assertEqual(fk.intent.get("references"), "orgs")

    def test_raw_sql_carries_no_intent(self):
        (statement,) = self.render(ddl.sql("SELECT 1"))
        self.assertNotIsInstance(statement, MigrationStatement)


class DiffIntentSweepTestCase(unittest.TestCase):
    """
    Every up statement the diff generates, in every diff test, carries an
    Intent of a known kind.
    """

    def test_every_generated_statement_carries_an_intent(self):
        missing = []
        seen_kinds = set()
        real_migration = autogenerate_module.Migration

        def checking_migration(*args, **kwargs):
            migration = real_migration(*args, **kwargs)
            for statement in migration.up:
                intent = getattr(statement, "intent", None)
                if intent is None or intent.kind not in INTENT_KINDS:
                    missing.append(str(statement))
                else:
                    seen_kinds.add(intent.kind)
            return migration

        suite = unittest.TestSuite()
        loader = unittest.TestLoader()
        for name in DIFF_TEST_MODULES:
            suite.addTests(loader.loadTestsFromModule(importlib.import_module(name)))
        with mock.patch.object(autogenerate_module, "Migration", checking_migration):
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
                suite
            )
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertGreater(result.testsRun, 500)
        self.assertEqual(missing, [])
        # The sweep reaches the diff's main paths; a path that stops
        # generating would show up here as a kind that went missing.
        self.assertLessEqual(
            {
                "create_table",
                "add_column",
                "drop_column",
                "alter_column_type",
                "set_not_null",
                "backfill",
                "create_index",
                "drop_index",
                "add_foreign_key",
                "add_check",
                "rebuild_table",
                "rename_column",
                "rename_table",
                "session_setting",
            },
            seen_kinds,
        )


if __name__ == "__main__":
    unittest.main()
