import unittest

from src.sustained import Model


class TestLambdaJoinBuilder(unittest.TestCase):
    def setUp(self):
        class User(Model):
            tableName = "users"

        self.User = User

    def test_join_with_lambda_single_on(self):
        query = self.User.query().join(
            "accounts", lambda j: j.on("users.id", "=", "accounts.user_id")
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM users JOIN accounts ON users.id = accounts.user_id",
        )

    def test_join_with_lambda_multiple_on(self):
        query = self.User.query().join(
            "accounts",
            lambda j: j.on("users.id", "=", "accounts.user_id").on(
                "users.type", "=", "accounts.type"
            ),
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM users JOIN accounts ON users.id = accounts.user_id AND users.type = accounts.type",
        )

    def test_join_with_lambda_or_on(self):
        query = self.User.query().join(
            "accounts",
            lambda j: j.on("accounts.id", "=", "users.account_id").orOn(
                "accounts.owner_id", "=", "users.id"
            ),
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM users JOIN accounts ON accounts.id = users.account_id OR accounts.owner_id = users.id",
        )

    def test_join_with_lambda_and_on_and_or_on(self):
        query = self.User.query().join(
            "accounts",
            lambda j: j.on("accounts.id", "=", "users.account_id")
            .andOn("accounts.enabled", "=", "1")
            .orOn("accounts.owner_id", "=", "users.id"),
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM users JOIN accounts ON accounts.id = users.account_id AND accounts.enabled = 1 OR accounts.owner_id = users.id",
        )

    def test_join_with_lambda_and_on_before_on_raises(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "Cannot use 'andOn' for the first join condition. Use 'on' instead.",
        ):
            self.User.query().join(
                "accounts", lambda j: j.andOn("users.id", "=", "accounts.user_id")
            )

    def test_join_with_lambda_or_on_before_on_raises(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "Cannot use 'orOn' for the first join condition. Use 'on' instead.",
        ):
            self.User.query().join(
                "accounts", lambda j: j.orOn("users.id", "=", "accounts.user_id")
            )

    def test_join_with_lambda_no_conditions_raises(self):
        with self.assertRaisesRegex(
            RuntimeError, "A join condition must be specified inside the lambda."
        ):
            str(self.User.query().join("accounts", lambda j: None))

    def test_join_with_invalid_args_raises(self):
        with self.assertRaisesRegex(ValueError, "Invalid arguments for join method"):
            self.User.query().join("accounts", "one", "two")


if __name__ == "__main__":
    unittest.main()
