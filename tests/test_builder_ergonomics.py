"""
Tests for clone(), page(), snake_case method aliases, and window function
arguments and frames.
"""

import unittest

from sustained import create_model

User = create_model("ErgoUser", "users")


class TestClone(unittest.TestCase):
    def test_clone_is_independent(self):
        base = User.query().where("a", "=", 1)
        branched = base.clone().where("b", "=", 2)
        self.assertEqual(str(base), "SELECT * FROM users WHERE a = 1")
        self.assertEqual(str(branched), "SELECT * FROM users WHERE a = 1 AND b = 2")


class TestPage(unittest.TestCase):
    def test_page_zero_is_first_page(self):
        self.assertTrue(str(User.query().page(0, 25)).endswith("LIMIT 25 OFFSET 0"))

    def test_page_offsets_by_page_size(self):
        self.assertTrue(str(User.query().page(2, 25)).endswith("LIMIT 25 OFFSET 50"))

    def test_page_rejects_negative(self):
        with self.assertRaises(ValueError):
            User.query().page(-1, 25)


class TestSnakeCaseAliases(unittest.TestCase):
    def test_where_in(self):
        sql = str(User.query().where_in("id", [1, 2]))
        self.assertIn("id IN (1, 2)", sql)

    def test_order_by_and_group_by(self):
        sql = str(User.query().group_by("name").order_by("name", "desc"))
        self.assertIn("GROUP BY name", sql)
        self.assertIn("ORDER BY name DESC", sql)

    def test_union_all(self):
        sql = str(User.query().union_all(User.query()))
        self.assertIn("UNION ALL", sql)

    def test_left_join(self):
        sql = str(User.query().left_join("pets", "users.id", "=", "pets.owner_id"))
        self.assertIn("LEFT JOIN pets", sql)

    def test_unknown_snake_name_raises(self):
        with self.assertRaises(AttributeError):
            User.query().not_a_method

    def test_where_null_alias(self):
        sql = str(User.query().where_null("deleted_at"))
        self.assertIn("deleted_at IS NULL", sql)


class TestWindowFunctions(unittest.TestCase):
    def test_window_with_args(self):
        sql = str(
            User.query().select_window(
                "LAG", "prev_price", args=["price", 1], order_by=["day"]
            )
        )
        self.assertIn("LAG(price, 1) OVER (ORDER BY day) AS prev_price", sql)

    def test_window_with_frame(self):
        sql = str(
            User.query().select_window(
                "SUM",
                "running_total",
                args=["amount"],
                order_by=["day"],
                frame="ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
            )
        )
        self.assertIn(
            "SUM(amount) OVER (ORDER BY day ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_total",
            sql,
        )

    def test_window_order_by_direction(self):
        sql = str(
            User.query().select_window("ROW_NUMBER", "rn", order_by=["created DESC"])
        )
        self.assertIn("OVER (ORDER BY created DESC) AS rn", sql)


if __name__ == "__main__":
    unittest.main()
