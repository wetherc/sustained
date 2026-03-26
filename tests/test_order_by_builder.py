import unittest

from src.sustained.builders.order_by_builder import OrderByClauseBuilder
from src.sustained.model import Model


class TestOrderByClauseBuilder(unittest.TestCase):
    def setUp(self):
        class TestModel(Model):
            tableName = "test_table"

        self.model_class = TestModel
        self.builder = OrderByClauseBuilder(self.model_class)

    def test_single_order_by_asc(self):
        self.builder.orderBy("name")
        self.assertEqual(str(self.builder), "ORDER BY name ASC")

    def test_single_order_by_desc(self):
        self.builder.orderBy("age", "desc")
        self.assertEqual(str(self.builder), "ORDER BY age DESC")

    def test_multiple_order_by_clauses(self):
        self.builder.orderBy("name").orderBy("age", "desc").orderBy("city")
        self.assertEqual(str(self.builder), "ORDER BY name ASC, age DESC, city ASC")

    def test_empty_order_by(self):
        self.assertEqual(str(self.builder), "")

    def test_invalid_direction_raises_error(self):
        with self.assertRaisesRegex(
            ValueError, "Order by direction must be 'asc' or 'desc'."
        ):
            self.builder.orderBy("column", "invalid")

    def test_direction_case_insensitivity(self):
        self.builder.orderBy("col1", "ASC").orderBy("col2", "desc")
        self.assertEqual(str(self.builder), "ORDER BY col1 ASC, col2 DESC")


if __name__ == "__main__":
    unittest.main()
