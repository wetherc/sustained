"""
Tests for the MySQL and MariaDB dialect's SQL generation.
"""

import unittest

from sustained import DialectError, Model
from sustained.dialects import Dialects
from sustained.schema import (
    BigInteger,
    Boolean,
    Float,
    Integer,
    Json,
    Numeric,
    String,
    Text,
    Timestamp,
    build_create_table_sql,
)
from sustained.types import Expression


class Person(Model):
    tableName = "person"
    _dialect = Dialects.MYSQL


def compiler():
    return Dialects.get_compiler(Dialects.MYSQL)


class TestQuotingAndValues(unittest.TestCase):
    def test_identifiers_take_backticks(self):
        self.assertIn("FROM `person`", str(Person.query()))

    def test_fully_qualified_name_quoting(self):
        class User(Model):
            database = "db"
            tableName = "users"
            _dialect = Dialects.MYSQL

        self.assertIn("FROM `db`.`users`", str(User.query()))

    def test_placeholder_is_percent_s(self):
        sql, params = Person.query().where("name", "=", "Ada").to_sql()
        self.assertIn("`name` = %s", sql)
        self.assertEqual(params, ("Ada",))

    def test_backslash_is_escaped_in_a_literal(self):
        self.assertEqual(compiler().format_value("C:\\path"), "'C:\\\\path'")

    def test_quote_is_escaped_in_a_literal(self):
        self.assertEqual(compiler().format_value("O'Hara"), "'O''Hara'")

    def test_booleans_render_as_keywords(self):
        self.assertIn("`active` = TRUE", str(Person.query().where("active", "=", True)))

    def test_expressions_pass_through(self):
        self.assertEqual(compiler().format_value(Expression("NOW()")), "NOW()")


class TestLimitAndOffset(unittest.TestCase):
    def test_limit_alone(self):
        self.assertTrue(str(Person.query().limit(10)).endswith("LIMIT 10"))

    def test_limit_with_offset(self):
        self.assertTrue(
            str(Person.query().limit(10).offset(5)).endswith("LIMIT 10 OFFSET 5")
        )

    def test_offset_alone_asks_for_every_row(self):
        # MySQL rejects OFFSET without LIMIT, so the row cap is the
        # largest count the server accepts.
        self.assertTrue(
            str(Person.query().offset(5)).endswith(
                "LIMIT 18446744073709551615 OFFSET 5"
            )
        )

    def test_neither_renders_nothing(self):
        self.assertEqual(compiler().compile_limit_offset(None, None), "")

    def test_top_raises(self):
        with self.assertRaises(DialectError):
            str(Person.query().top(10))


class TestColumnTypes(unittest.TestCase):
    def test_types_render_in_the_catalog_spelling(self):
        expected = {
            "INT": Integer(),
            "BIGINT": BigInteger(),
            "VARCHAR(120)": String(120),
            "TEXT": Text(),
            "TINYINT(1)": Boolean(),
            "DOUBLE": Float(),
            "DECIMAL(18, 6)": Numeric(),
            "DATETIME": Timestamp(),
            "JSON": Json(),
        }
        for sql, coldef in expected.items():
            with self.subTest(type=sql):
                self.assertEqual(compiler().compile_column_type(coldef), sql)

    def test_autoincrement_renders_auto_increment(self):
        sql = build_create_table_sql(
            compiler(),
            "`users`",
            {"id": Integer(primary_key=True, autoincrement=True)},
        )
        self.assertEqual(
            sql, "CREATE TABLE `users` (`id` INT AUTO_INCREMENT PRIMARY KEY)"
        )

    def test_create_table_renders_every_attribute(self):
        sql = build_create_table_sql(
            compiler(),
            "`users`",
            {
                "id": Integer(primary_key=True, autoincrement=True),
                "email": String(120, nullable=False, unique=True),
                "seen_at": Timestamp(default=Expression("CURRENT_TIMESTAMP")),
            },
        )
        self.assertEqual(
            sql,
            "CREATE TABLE `users` ("
            "`id` INT AUTO_INCREMENT PRIMARY KEY, "
            "`email` VARCHAR(120) NOT NULL UNIQUE, "
            "`seen_at` DATETIME DEFAULT CURRENT_TIMESTAMP)",
        )


class TestColumnValidation(unittest.TestCase):
    def test_unique_text_column_raises(self):
        with self.assertRaises(DialectError) as caught:
            compiler().validate_column_def(Text(unique=True))
        self.assertIn("prefix length", str(caught.exception))

    def test_text_primary_key_raises(self):
        with self.assertRaises(DialectError):
            compiler().validate_column_def(Text(primary_key=True))

    def test_default_on_a_json_column_raises(self):
        with self.assertRaises(DialectError) as caught:
            compiler().validate_column_def(Json(default="{}"))
        self.assertIn("no literal DEFAULT", str(caught.exception))

    def test_plain_text_column_passes(self):
        self.assertIsNone(compiler().validate_column_def(Text()))

    def test_a_sized_string_may_be_unique(self):
        self.assertIsNone(compiler().validate_column_def(String(120, unique=True)))


class TestUpsert(unittest.TestCase):
    def test_merge_assigns_from_values(self):
        sql = (
            Person.query()
            .insert({"id": 1, "name": "Ada"})
            .onConflict("id")
            .merge()
            .to_sql()[0]
        )
        self.assertIn("ON DUPLICATE KEY UPDATE `name` = VALUES(`name`)", sql)

    def test_ignore_assigns_the_conflict_column_to_itself(self):
        sql = (
            Person.query()
            .insert({"id": 1, "name": "Ada"})
            .onConflict("id")
            .ignore()
            .to_sql()[0]
        )
        self.assertIn("ON DUPLICATE KEY UPDATE `id` = `id`", sql)

    def test_ignore_falls_back_to_the_first_column(self):
        sql = compiler().compile_upsert_statement(
            "`t`", ["a", "b"], ["(1, 2)"], [], "ignore", []
        )
        self.assertTrue(sql.endswith("ON DUPLICATE KEY UPDATE `a` = `a`"))


class TestUnsupported(unittest.TestCase):
    def test_returning_raises(self):
        with self.assertRaises(DialectError) as caught:
            compiler().compile_returning("`id`")
        self.assertIn("LAST_INSERT_ID()", str(caught.exception))

    def test_distinct_on_raises(self):
        with self.assertRaises(DialectError):
            compiler().compile_distinct_on(["`a`"])

    def test_qualify_is_not_supported(self):
        self.assertFalse(compiler().supports_qualify())

    def test_string_agg_raises(self):
        with self.assertRaises(DialectError):
            Person.query().select_func("STRING_AGG", "name")


class TestSchemaStatements(unittest.TestCase):
    def test_alter_column_type_uses_modify(self):
        self.assertEqual(
            compiler().compile_alter_column_type("`t`", "c", "BIGINT"),
            ["ALTER TABLE `t` MODIFY COLUMN `c` BIGINT"],
        )

    def test_alter_nullability_restates_the_type(self):
        self.assertEqual(
            compiler().compile_alter_column_nullability("`t`", "c", "INT", False),
            ["ALTER TABLE `t` MODIFY COLUMN `c` INT NOT NULL"],
        )
        self.assertEqual(
            compiler().compile_alter_column_nullability("`t`", "c", "INT", True),
            ["ALTER TABLE `t` MODIFY COLUMN `c` INT NULL"],
        )

    def test_drop_index_names_its_table(self):
        self.assertEqual(
            compiler().compile_drop_index("ix_name", "`t`"),
            "DROP INDEX `ix_name` ON `t`",
        )

    def test_add_and_rename_follow_the_standard_spelling(self):
        self.assertEqual(
            compiler().compile_add_column("`t`", "`c` INT"),
            "ALTER TABLE `t` ADD COLUMN `c` INT",
        )
        self.assertEqual(
            compiler().compile_rename_column("`t`", "old", "new"),
            "ALTER TABLE `t` RENAME COLUMN `old` TO `new`",
        )

    def test_recursive_cte_keyword(self):
        self.assertEqual(compiler().compile_with_keyword(True), "WITH RECURSIVE")


class TestForeignKeys(unittest.TestCase):
    def test_references_are_not_written_beside_the_column(self):
        self.assertFalse(compiler().inline_references())

    def test_other_dialects_still_write_them_beside_the_column(self):
        for dialect in Dialects:
            if dialect is Dialects.MYSQL:
                continue
            with self.subTest(dialect=dialect.name):
                self.assertTrue(Dialects.get_compiler(dialect).inline_references())

    def test_create_table_carries_the_key_as_a_constraint(self):
        sql = build_create_table_sql(
            compiler(),
            "`shows`",
            {
                "id": Integer(primary_key=True),
                "venue_id": Integer(references="venues.id"),
            },
        )
        self.assertEqual(
            sql,
            "CREATE TABLE `shows` (`id` INT PRIMARY KEY, `venue_id` INT, "
            "FOREIGN KEY (`venue_id`) REFERENCES `venues` (`id`))",
        )

    def test_a_named_key_added_and_dropped(self):
        self.assertEqual(
            compiler().compile_add_foreign_key(
                "`shows`", "fk_shows_venue_id", "venue_id", "`venues`", "id"
            ),
            "ALTER TABLE `shows` ADD CONSTRAINT `fk_shows_venue_id` "
            "FOREIGN KEY (`venue_id`) REFERENCES `venues` (`id`)",
        )
        self.assertEqual(
            compiler().compile_drop_foreign_key("`shows`", "fk_shows_venue_id"),
            "ALTER TABLE `shows` DROP FOREIGN KEY `fk_shows_venue_id`",
        )


class TestLockingAndTransactions(unittest.TestCase):
    def test_row_locking(self):
        self.assertEqual(compiler().compile_locking(False, False), "FOR UPDATE")
        self.assertEqual(
            compiler().compile_locking(True, False), "FOR UPDATE SKIP LOCKED"
        )
        self.assertEqual(compiler().compile_locking(False, True), "FOR UPDATE NOWAIT")

    def test_advisory_lock_statements(self):
        self.assertEqual(
            compiler().migration_lock_sql("sustained_migrations"),
            ["SELECT GET_LOCK('sustained_migrations', -1)"],
        )
        self.assertEqual(
            compiler().migration_unlock_sql("sustained_migrations"),
            ["SELECT RELEASE_LOCK('sustained_migrations')"],
        )


class TestFunctionNames(unittest.TestCase):
    def test_now_keeps_its_name(self):
        sql = str(Person.query().now(alias="t"))
        self.assertIn("NOW() AS `t`", sql)

    def test_getdate_translates_to_now(self):
        sql = str(Person.query().getdate(alias="t"))
        self.assertIn("NOW() AS `t`", sql)

    def test_length_keeps_its_name(self):
        sql = str(Person.query().select_func("LENGTH", "name", alias="n"))
        self.assertIn("LENGTH(`name`) AS `n`", sql)


if __name__ == "__main__":
    unittest.main()
