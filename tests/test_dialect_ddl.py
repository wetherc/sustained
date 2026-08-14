"""
String-level tests for dialect-specific ALTER, RENAME, and INDEX SQL.
"""

import unittest

from sustained.compilers.duckdb import DuckDbCompiler
from sustained.compilers.mssql import MssqlCompiler
from sustained.compilers.postgres import PostgresCompiler
from sustained.dialects import Dialects
from sustained.exceptions import DialectError


class TestPostgresAlter(unittest.TestCase):
    def setUp(self):
        self.c = PostgresCompiler(Dialects.POSTGRES)

    def test_alter_type_with_using(self):
        self.assertEqual(
            self.c.compile_alter_column_type('"t"', "n", "INTEGER", "n::integer"),
            ['ALTER TABLE "t" ALTER COLUMN "n" TYPE INTEGER USING n::integer'],
        )

    def test_alter_nullability(self):
        self.assertEqual(
            self.c.compile_alter_column_nullability('"t"', "n", "INTEGER", False),
            ['ALTER TABLE "t" ALTER COLUMN "n" SET NOT NULL'],
        )
        self.assertEqual(
            self.c.compile_alter_column_nullability('"t"', "n", "INTEGER", True),
            ['ALTER TABLE "t" ALTER COLUMN "n" DROP NOT NULL'],
        )

    def test_rename_and_index(self):
        self.assertEqual(
            self.c.compile_rename_column('"t"', "a", "b"),
            'ALTER TABLE "t" RENAME COLUMN "a" TO "b"',
        )
        self.assertEqual(
            self.c.compile_create_index("ix", '"t"', ["a", "b"], True),
            'CREATE UNIQUE INDEX "ix" ON "t" ("a", "b")',
        )
        self.assertEqual(self.c.compile_drop_index("ix", '"t"'), 'DROP INDEX "ix"')


class TestMssqlAlter(unittest.TestCase):
    def setUp(self):
        self.c = MssqlCompiler(Dialects.MSSQL)

    def test_alter_type_restates_definition(self):
        self.assertEqual(
            self.c.compile_alter_column_type("[t]", "n", "NVARCHAR(50)"),
            ["ALTER TABLE [t] ALTER COLUMN [n] NVARCHAR(50)"],
        )

    def test_alter_nullability_includes_type(self):
        self.assertEqual(
            self.c.compile_alter_column_nullability("[t]", "n", "INT", False),
            ["ALTER TABLE [t] ALTER COLUMN [n] INT NOT NULL"],
        )

    def test_sp_rename(self):
        self.assertEqual(
            self.c.compile_rename_column("[dbo].[t]", "a", "b"),
            "EXEC sp_rename 'dbo.t.a', 'b', 'COLUMN'",
        )
        self.assertEqual(
            self.c.compile_rename_table("[t]", "[t2]"),
            "EXEC sp_rename 't', 't2'",
        )

    def test_drop_index_requires_table(self):
        self.assertEqual(
            self.c.compile_drop_index("ix", "[t]"), "DROP INDEX [ix] ON [t]"
        )


class TestDuckDbAlter(unittest.TestCase):
    def setUp(self):
        self.c = DuckDbCompiler(Dialects.DUCKDB)

    def test_alter_type(self):
        self.assertEqual(
            self.c.compile_alter_column_type('"t"', "n", "INTEGER"),
            ['ALTER TABLE "t" ALTER COLUMN "n" SET DATA TYPE INTEGER'],
        )

    def test_alter_nullability(self):
        self.assertEqual(
            self.c.compile_alter_column_nullability('"t"', "n", "INTEGER", False),
            ['ALTER TABLE "t" ALTER COLUMN "n" SET NOT NULL'],
        )


class TestDefaultDialectAlter(unittest.TestCase):
    def test_alter_unsupported(self):
        from sustained.compilers.base import Compiler

        c = Compiler(Dialects.DEFAULT)
        self.assertFalse(c.supports_alter_column())
        with self.assertRaises(DialectError):
            c.compile_alter_column_type("t", "n", "INTEGER")
        with self.assertRaises(DialectError):
            c.compile_alter_column_nullability("t", "n", "INTEGER", False)


if __name__ == "__main__":
    unittest.main()
