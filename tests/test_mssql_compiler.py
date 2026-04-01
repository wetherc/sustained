import unittest

from sustained import Model
from sustained.dialects import Dialects


class Person(Model):
    tableName = "person"


class TestMssqlCompiler(unittest.TestCase):
    def setUp(self):
        Person.set_dialect(Dialects.MSSQL)

    def tearDown(self):
        Person.set_dialect(Dialects.DEFAULT)

    def test_select_with_limit(self):
        query = Person.query().limit(10)
        self.assertEqual(
            str(query),
            "SELECT * FROM [person] OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY",
        )

    def test_select_with_offset(self):
        query = Person.query().offset(5)
        self.assertEqual(
            str(query),
            "SELECT * FROM [person] OFFSET 5 ROWS",
        )

    def test_select_with_limit_and_offset(self):
        query = Person.query().limit(10).offset(5)
        self.assertEqual(
            str(query),
            "SELECT * FROM [person] OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY",
        )

    def test_select_with_top(self):
        query = Person.query().top(10)
        self.assertEqual(
            str(query),
            "SELECT TOP 10 * FROM [person]",
        )

    def test_table_name_quoting(self):
        query = Person.query()
        self.assertIn("FROM [person]", str(query))

    def test_fully_qualified_name_quoting(self):
        class User(Model):
            database = "db"
            tableSchema = "schema"
            tableName = "users"

        User.set_dialect(Dialects.MSSQL)
        query = User.query()
        self.assertIn("FROM [db].[schema].[users]", str(query))
        User.set_dialect(Dialects.DEFAULT)
