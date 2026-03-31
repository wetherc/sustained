import unittest

from sustained.dialects import Dialects
from sustained.model import create_model


class TestDialect(unittest.TestCase):
    def test_default_dialect(self):
        Person = create_model("Person", "persons")
        query = Person.query().select("name")
        self.assertEqual(str(query), "SELECT name FROM persons")

    def test_set_dialect_presto(self):
        Person = create_model("Person", "persons")
        Person.set_dialect(Dialects.PRESTO)
        query = Person.query().select("name")
        self.assertEqual(str(query), 'SELECT "name" FROM "persons"')

    def test_from_clause_quoting(self):
        Person = create_model("Person", "persons")
        query = Person.query().select("name").from_("my_schema.my_table", alias="p")
        self.assertEqual(str(query), "SELECT name FROM my_schema.my_table AS p")


if __name__ == "__main__":
    unittest.main()
