import unittest

from sustained import Model
from sustained.dialects import Dialects


class Person(Model):
    tableName = "person"


class TestPostgresCompiler(unittest.TestCase):
    def setUp(self):
        Person.set_dialect(Dialects.POSTGRES)

    def tearDown(self):
        Person.set_dialect(Dialects.DEFAULT)

    def test_select_with_limit(self):
        query = Person.query().limit(10)
        self.assertEqual(
            str(query),
            'SELECT * FROM "person" LIMIT 10',
        )

    def test_select_with_offset(self):
        query = Person.query().offset(5)
        self.assertEqual(
            str(query),
            'SELECT * FROM "person" OFFSET 5',
        )

    def test_select_with_limit_and_offset(self):
        query = Person.query().limit(10).offset(5)
        self.assertEqual(
            str(query),
            'SELECT * FROM "person" LIMIT 10 OFFSET 5',
        )

    def test_select_with_top_is_ignored(self):
        query = Person.query().top(10)
        self.assertEqual(
            str(query),
            'SELECT * FROM "person"',
        )

    def test_table_name_quoting(self):
        query = Person.query()
        self.assertIn('FROM "person"', str(query))

    def test_fully_qualified_name_quoting(self):
        class User(Model):
            database = "db"
            tableSchema = "schema"
            tableName = "users"

        User.set_dialect(Dialects.POSTGRES)
        query = User.query()
        self.assertIn('FROM "db"."schema"."users"', str(query))
        User.set_dialect(Dialects.DEFAULT)
