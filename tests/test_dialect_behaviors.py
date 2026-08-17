"""
Tests for dialect-specific SQL generation rules.
"""

import unittest

from sustained import DialectError, Model
from sustained.dialects import Dialects


class PgPerson(Model):
    tableName = "person"
    _dialect = Dialects.POSTGRES


class MsPerson(Model):
    tableName = "person"
    _dialect = Dialects.MSSQL


class PrPerson(Model):
    tableName = "person"
    _dialect = Dialects.PRESTO


class DefPerson(Model):
    tableName = "person"
    _dialect = Dialects.DEFAULT


class TestIlikeCompilation(unittest.TestCase):
    def test_postgres_uses_native_ilike(self):
        sql = str(PgPerson.query().whereILike("name", "j%"))
        self.assertIn("\"name\" ILIKE 'j%'", sql)

    def test_default_emulates_ilike(self):
        sql = str(DefPerson.query().whereILike("name", "j%"))
        self.assertIn("LOWER(name) LIKE LOWER('j%')", sql)

    def test_mssql_emulates_ilike(self):
        sql = str(MsPerson.query().whereILike("name", "j%"))
        self.assertIn("LOWER([name]) LIKE LOWER('j%')", sql)

    def test_where_with_ilike_operator_routes_through_compiler(self):
        sql = str(DefPerson.query().where("name", "ilike", "j%"))
        self.assertIn("LOWER(name) LIKE LOWER('j%')", sql)


class TestBooleanCompilation(unittest.TestCase):
    def test_default_renders_keywords(self):
        sql = str(DefPerson.query().where("active", "=", True))
        self.assertIn("active = TRUE", sql)

    def test_mssql_renders_bits(self):
        sql = str(MsPerson.query().where("active", "=", False))
        self.assertIn("[active] = 0", sql)


class TestTopCompilation(unittest.TestCase):
    def test_top_raises_on_postgres(self):
        with self.assertRaises(DialectError):
            str(PgPerson.query().top(10))

    def test_top_raises_on_presto(self):
        with self.assertRaises(DialectError):
            str(PrPerson.query().top(10))

    def test_top_works_on_mssql(self):
        self.assertIn("TOP 10", str(MsPerson.query().top(10)))


class TestTransactionalDdl(unittest.TestCase):
    # Whether a schema change taken back by a rollback really goes away,
    # named per dialect so a new one has to state its answer.
    EXPECTED = {
        Dialects.DEFAULT: True,
        Dialects.POSTGRES: True,
        Dialects.DUCKDB: True,
        Dialects.MSSQL: True,
        Dialects.PRESTO: True,
        Dialects.ATHENA: False,
    }

    def test_every_dialect_states_an_answer(self):
        self.assertEqual(set(self.EXPECTED), set(Dialects))

    def test_matches_the_expected_answer(self):
        for dialect, expected in self.EXPECTED.items():
            with self.subTest(dialect=dialect.name):
                compiler = Dialects.get_compiler(dialect)
                self.assertEqual(compiler.supports_transactional_ddl(), expected)

    def test_no_transactions_means_no_transactional_ddl(self):
        for dialect in Dialects:
            compiler = Dialects.get_compiler(dialect)
            if not compiler.supports_transactions():
                with self.subTest(dialect=dialect.name):
                    self.assertFalse(compiler.supports_transactional_ddl())


class TestPrestoLimitOffset(unittest.TestCase):
    def test_offset_precedes_limit(self):
        sql = str(PrPerson.query().limit(10).offset(5))
        self.assertTrue(sql.endswith("OFFSET 5 LIMIT 10"))


if __name__ == "__main__":
    unittest.main()
