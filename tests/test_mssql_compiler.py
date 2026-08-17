import unittest

from sustained import DialectError, Model
from sustained.dialects import Dialects


class Person(Model):
    tableName = "person"


class TestMssqlCompiler(unittest.TestCase):
    def setUp(self):
        Person.set_dialect(Dialects.MSSQL)

    def tearDown(self):
        Person.set_dialect(Dialects.DEFAULT)

    def test_select_with_limit(self):
        query = Person.query().orderBy("id").limit(10)
        self.assertEqual(
            str(query),
            "SELECT * FROM [person] ORDER BY [id] ASC OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY",
        )

    def test_select_with_limit_requires_order_by(self):
        query = Person.query().limit(10)
        with self.assertRaises(DialectError):
            str(query)

    def test_select_with_offset(self):
        query = Person.query().orderBy("id").offset(5)
        self.assertEqual(
            str(query),
            "SELECT * FROM [person] ORDER BY [id] ASC OFFSET 5 ROWS",
        )

    def test_select_with_offset_requires_order_by(self):
        query = Person.query().offset(5)
        with self.assertRaises(DialectError):
            str(query)

    def test_select_with_limit_and_offset(self):
        query = Person.query().orderBy("id").limit(10).offset(5)
        self.assertEqual(
            str(query),
            "SELECT * FROM [person] ORDER BY [id] ASC OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY",
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


class TestMssqlCreateTable(unittest.TestCase):
    """SQL Server has no IF NOT EXISTS clause on CREATE TABLE."""

    def setUp(self):
        from sustained.schema import Integer, String

        self.columns = {"id": Integer(primary_key=True), "name": String(20)}
        self.compiler = Dialects.get_compiler(Dialects.MSSQL)

    def build(self, if_not_exists):
        from sustained.schema import build_create_table_sql

        return build_create_table_sql(
            self.compiler, "[widgets]", self.columns, if_not_exists=if_not_exists
        )

    def test_a_plain_create_table_has_no_guard(self):
        self.assertTrue(self.build(False).startswith("CREATE TABLE [widgets] ("))

    def test_creating_only_when_missing_checks_the_catalog(self):
        sql = self.build(True)
        self.assertTrue(sql.startswith("IF OBJECT_ID('[widgets]', 'U') IS NULL "))
        self.assertNotIn("IF NOT EXISTS", sql)

    def test_other_dialects_keep_the_clause(self):
        from sustained.schema import build_create_table_sql

        sql = build_create_table_sql(
            Dialects.get_compiler(Dialects.POSTGRES),
            '"widgets"',
            self.columns,
            if_not_exists=True,
        )
        self.assertTrue(sql.startswith('CREATE TABLE IF NOT EXISTS "widgets" ('))
