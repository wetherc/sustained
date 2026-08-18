"""
String-level tests for dialect-specific ALTER, RENAME, INDEX, enum type,
and table constraint SQL.
"""

import unittest

from sustained.compilers.base import Compiler
from sustained.compilers.duckdb import DuckDbCompiler
from sustained.compilers.mssql import MssqlCompiler
from sustained.compilers.mysql import MysqlCompiler
from sustained.compilers.postgres import PostgresCompiler
from sustained.dialects import Dialects
from sustained.exceptions import DialectError
from sustained.schema import Enum


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


class TestEnumTypeSql(unittest.TestCase):
    def test_postgres_creates_drops_and_extends_a_type(self):
        c = PostgresCompiler(Dialects.POSTGRES)
        self.assertEqual(
            c.compile_create_enum_type("mood", ["sad", "ok"]),
            "CREATE TYPE \"mood\" AS ENUM ('sad', 'ok')",
        )
        self.assertEqual(c.compile_drop_enum_type("mood"), 'DROP TYPE "mood"')
        self.assertEqual(
            c.compile_drop_enum_type("mood", if_exists=True),
            'DROP TYPE IF EXISTS "mood"',
        )
        self.assertEqual(
            c.compile_add_enum_value("mood", "great"),
            "ALTER TYPE \"mood\" ADD VALUE 'great'",
        )

    def test_duckdb_creates_and_drops_a_type(self):
        c = DuckDbCompiler(Dialects.DUCKDB)
        self.assertEqual(
            c.compile_create_enum_type("mood", ["sad", "ok"]),
            "CREATE TYPE \"mood\" AS ENUM ('sad', 'ok')",
        )
        self.assertEqual(c.compile_drop_enum_type("mood"), 'DROP TYPE "mood"')

    def test_mysql_renders_the_value_list_inline(self):
        c = MysqlCompiler(Dialects.MYSQL)
        column = Enum("sad", "ok", name="mood")
        self.assertEqual(c.compile_column_type(column), "ENUM('sad', 'ok')")

    def test_check_strategy_sizes_a_varchar_to_the_longest_value(self):
        c = Compiler(Dialects.DEFAULT)
        column = Enum("sad", "great", name="mood")
        self.assertEqual(c.compile_column_type(column), "VARCHAR(5)")

    def test_default_dialect_has_no_type_statements(self):
        c = Compiler(Dialects.DEFAULT)
        with self.assertRaises(DialectError):
            c.compile_create_enum_type("mood", ["sad"])
        with self.assertRaises(DialectError):
            c.compile_drop_enum_type("mood")
        with self.assertRaises(DialectError):
            c.compile_add_enum_value("mood", "ok")

    def test_presto_and_athena_refuse_the_column(self):
        for dialect in (Dialects.PRESTO, Dialects.ATHENA):
            compiler = Dialects.get_compiler(dialect)
            with self.assertRaises(DialectError):
                compiler.validate_column_def(Enum("sad", name="mood"))


class TestConstraintSql(unittest.TestCase):
    def setUp(self):
        self.c = Compiler(Dialects.DEFAULT)

    def test_add_and_drop_check(self):
        self.assertEqual(
            self.c.compile_add_check("t", "ck_price", "price > 0"),
            "ALTER TABLE t ADD CONSTRAINT ck_price CHECK (price > 0)",
        )
        self.assertEqual(
            self.c.compile_drop_constraint("t", "ck_price"),
            "ALTER TABLE t DROP CONSTRAINT ck_price",
        )

    def test_add_foreign_key_with_actions(self):
        self.assertEqual(
            self.c.compile_add_foreign_key(
                "t", "fk_t_o", "owner_id", "owners", "id", "CASCADE", "SET NULL"
            ),
            "ALTER TABLE t ADD CONSTRAINT fk_t_o FOREIGN KEY (owner_id) "
            "REFERENCES owners (id) ON DELETE CASCADE ON UPDATE SET NULL",
        )

    def test_add_foreign_key_without_target_columns(self):
        # An empty target list references the target's primary key.
        self.assertEqual(
            self.c.compile_add_foreign_key("t", "fk_t_o", "owner_id", "owners", ()),
            "ALTER TABLE t ADD CONSTRAINT fk_t_o FOREIGN KEY (owner_id) "
            "REFERENCES owners",
        )

    def test_mysql_drops_a_foreign_key_by_its_own_spelling(self):
        c = MysqlCompiler(Dialects.MYSQL)
        self.assertEqual(
            c.compile_drop_foreign_key("`t`", "fk_t_o"),
            "ALTER TABLE `t` DROP FOREIGN KEY `fk_t_o`",
        )

    def test_presto_and_athena_refuse_and_name_themselves(self):
        for dialect, display in (
            (Dialects.PRESTO, "Presto"),
            (Dialects.ATHENA, "Athena"),
        ):
            compiler = Dialects.get_compiler(dialect)
            with self.assertRaises(DialectError) as caught:
                compiler.compile_add_check("t", "ck", "x > 0")
            self.assertIn(f"{display} tables have no CHECK", str(caught.exception))
            with self.assertRaises(DialectError) as caught:
                compiler.compile_add_foreign_key("t", "fk", "a", "o", "id")
            self.assertIn(f"{display} tables have no foreign", str(caught.exception))


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
