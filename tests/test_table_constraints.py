"""
Tests for declared table constraints: the Check and ForeignKey classes,
their rendering into CREATE TABLE per dialect, the ALTER-time compiler
methods, and refusal on the dialects that enforce no constraints.
"""

import sqlite3
import unittest

from sustained import DialectError, Model
from sustained.dialects import Dialects
from sustained.schema import (
    Check,
    ForeignKey,
    Integer,
    String,
    build_create_table_sql,
)


class Owner(Model):
    tableName = "owners"
    tableColumns = {
        "id": Integer(primary_key=True),
        "name": String(80, nullable=False),
    }


class Pet(Model):
    tableName = "pets"
    tableColumns = {
        "id": Integer(primary_key=True),
        "owner_id": Integer(nullable=False),
        "age": Integer(),
    }
    tableConstraints = [
        Check("ck_pets_age_positive", "age >= 0"),
        ForeignKey(
            "fk_pets_owner",
            "owner_id",
            "owners.id",
            on_delete="CASCADE",
        ),
    ]


class TestCheck(unittest.TestCase):
    def test_records_name_and_expression(self):
        check = Check("ck_age", "age >= 0")
        self.assertEqual(check.name, "ck_age")
        self.assertEqual(check.expression, "age >= 0")

    def test_expression_whitespace_trimmed(self):
        self.assertEqual(Check("ck", "  a > 0  ").expression, "a > 0")

    def test_name_required(self):
        with self.assertRaisesRegex(ValueError, "needs a name"):
            Check("", "age >= 0")

    def test_expression_required(self):
        with self.assertRaisesRegex(ValueError, "needs a SQL expression"):
            Check("ck_age", "   ")


class TestForeignKey(unittest.TestCase):
    def test_single_column(self):
        fk = ForeignKey("fk_x", "owner_id", "owners.id")
        self.assertEqual(fk.columns, ("owner_id",))
        self.assertEqual(fk.target_table, "owners")
        self.assertEqual(fk.target_columns, ("id",))
        self.assertIsNone(fk.on_delete)
        self.assertIsNone(fk.on_update)

    def test_composite_columns(self):
        fk = ForeignKey("fk_x", ("a", "b"), ("t.c", "t.d"), on_update="NO ACTION")
        self.assertEqual(fk.columns, ("a", "b"))
        self.assertEqual(fk.target_table, "t")
        self.assertEqual(fk.target_columns, ("c", "d"))
        self.assertEqual(fk.on_update, "NO ACTION")

    def test_schema_qualified_target(self):
        fk = ForeignKey("fk_x", "owner_id", "app.owners.id")
        self.assertEqual(fk.target_table, "app.owners")
        self.assertEqual(fk.target_columns, ("id",))

    def test_name_required(self):
        with self.assertRaisesRegex(ValueError, "needs a name"):
            ForeignKey("", "a", "t.b")

    def test_columns_required(self):
        with self.assertRaisesRegex(ValueError, "at least one column"):
            ForeignKey("fk_x", (), ())

    def test_length_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, "constrains 2"):
            ForeignKey("fk_x", ("a", "b"), "t.c")

    def test_bare_target_rejected(self):
        with self.assertRaisesRegex(ValueError, "table.column"):
            ForeignKey("fk_x", "a", "owners")

    def test_mixed_target_tables_rejected(self):
        with self.assertRaisesRegex(ValueError, "more than one table"):
            ForeignKey("fk_x", ("a", "b"), ("t1.c", "t2.d"))

    def test_action_normalized(self):
        fk = ForeignKey("fk_x", "a", "t.b", on_delete="set  null")
        self.assertEqual(fk.on_delete, "SET NULL")

    def test_bad_action_rejected(self):
        with self.assertRaisesRegex(ValueError, "not a referential action"):
            ForeignKey("fk_x", "a", "t.b", on_delete="EXPLODE")


class TestCreateTableRendering(unittest.TestCase):
    def render(self, dialect):
        compiler = Dialects.get_compiler(dialect)
        return build_create_table_sql(
            compiler,
            compiler.quote_fully_qualified_identifier("pets"),
            Pet.tableColumns,
            constraints=Pet.tableConstraints,
        )

    def test_default_dialect(self):
        sql = self.render(Dialects.DEFAULT)
        self.assertIn("CONSTRAINT ck_pets_age_positive CHECK (age >= 0)", sql)
        self.assertIn(
            "CONSTRAINT fk_pets_owner FOREIGN KEY (owner_id) "
            "REFERENCES owners (id) ON DELETE CASCADE",
            sql,
        )

    def test_postgres_quoting(self):
        sql = self.render(Dialects.POSTGRES)
        self.assertIn(
            'CONSTRAINT "fk_pets_owner" FOREIGN KEY ("owner_id") '
            'REFERENCES "owners" ("id") ON DELETE CASCADE',
            sql,
        )

    def test_mysql_quoting(self):
        sql = self.render(Dialects.MYSQL)
        self.assertIn(
            "CONSTRAINT `fk_pets_owner` FOREIGN KEY (`owner_id`) "
            "REFERENCES `owners` (`id`) ON DELETE CASCADE",
            sql,
        )
        self.assertIn("CONSTRAINT `ck_pets_age_positive` CHECK (age >= 0)", sql)

    def test_mssql_quoting(self):
        sql = self.render(Dialects.MSSQL)
        self.assertIn(
            "CONSTRAINT [fk_pets_owner] FOREIGN KEY ([owner_id]) "
            "REFERENCES [owners] ([id]) ON DELETE CASCADE",
            sql,
        )

    def test_declared_constraints_render_last(self):
        sql = self.render(Dialects.DEFAULT)
        self.assertLess(sql.index("owner_id INTEGER"), sql.index("CONSTRAINT"))

    def test_composite_foreign_key(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        sql = build_create_table_sql(
            compiler,
            "orders",
            {"a": Integer(), "b": Integer()},
            constraints=[ForeignKey("fk_ab", ("a", "b"), ("t.c", "t.d"))],
        )
        self.assertIn("FOREIGN KEY (a, b) REFERENCES t (c, d)", sql)

    def test_presto_refuses(self):
        with self.assertRaisesRegex(DialectError, "no table\\s+constraints"):
            self.render(Dialects.PRESTO)

    def test_athena_refuses(self):
        compiler = Dialects.get_compiler(Dialects.ATHENA)
        with self.assertRaisesRegex(DialectError, "constraint"):
            build_create_table_sql(
                compiler,
                "pets",
                {"age": Integer()},
                constraints=[Check("ck_age", "age >= 0")],
            )


class TestModelIntegration(unittest.TestCase):
    def test_model_passes_constraints_through(self):
        sql = Pet.create_table_sql()
        self.assertIn("CONSTRAINT ck_pets_age_positive", sql)
        self.assertIn("CONSTRAINT fk_pets_owner", sql)

    def test_sqlite_enforces_check_and_foreign_key(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(Owner.create_table_sql())
        conn.execute(Pet.create_table_sql())
        conn.execute("INSERT INTO owners (id, name) VALUES (1, 'Ann')")
        conn.execute("INSERT INTO pets (id, owner_id, age) VALUES (1, 1, 3)")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO pets (id, owner_id, age) VALUES (2, 1, -1)")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO pets (id, owner_id, age) VALUES (3, 9, 2)")
        conn.execute("DELETE FROM owners WHERE id = 1")
        rows = conn.execute("SELECT count(*) FROM pets").fetchone()
        self.assertEqual(rows[0], 0)
        conn.close()


class TestAlterCompilerMethods(unittest.TestCase):
    def test_add_check(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        sql = compiler.compile_add_check('"pets"', "ck_age", "age >= 0")
        self.assertEqual(
            sql,
            'ALTER TABLE "pets" ADD CONSTRAINT "ck_age" CHECK (age >= 0)',
        )

    def test_add_foreign_key_old_signature(self):
        compiler = Dialects.get_compiler(Dialects.MYSQL)
        sql = compiler.compile_add_foreign_key(
            "`pets`", "fk_owner", "owner_id", "`owners`", "id"
        )
        self.assertEqual(
            sql,
            "ALTER TABLE `pets` ADD CONSTRAINT `fk_owner` FOREIGN KEY "
            "(`owner_id`) REFERENCES `owners` (`id`)",
        )

    def test_add_foreign_key_composite_with_actions(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        sql = compiler.compile_add_foreign_key(
            '"orders"',
            "fk_ab",
            ("a", "b"),
            '"t"',
            ("c", "d"),
            on_delete="SET NULL",
            on_update="CASCADE",
        )
        self.assertEqual(
            sql,
            'ALTER TABLE "orders" ADD CONSTRAINT "fk_ab" FOREIGN KEY '
            '("a", "b") REFERENCES "t" ("c", "d") '
            "ON DELETE SET NULL ON UPDATE CASCADE",
        )

    def test_presto_refuses_add_check(self):
        compiler = Dialects.get_compiler(Dialects.PRESTO)
        with self.assertRaisesRegex(DialectError, "Presto"):
            compiler.compile_add_check("t", "ck", "a > 0")

    def test_athena_refuses_add_foreign_key(self):
        compiler = Dialects.get_compiler(Dialects.ATHENA)
        with self.assertRaisesRegex(DialectError, "Athena"):
            compiler.compile_add_foreign_key("t", "fk", "a", "u", "b")


if __name__ == "__main__":
    unittest.main()
