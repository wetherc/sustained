import unittest

from src.sustained import Model, QueryBuilder, RelationType


class TestQueryBuilder(unittest.TestCase):
    def test_select_from(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("id", "name")
        self.assertEqual(str(query), "SELECT id, name FROM users")

    def test_join_related(self):
        class Person(Model):
            tableName = "persons"

        class Animal(Model):
            tableName = "animals"
            relationMappings = {
                "owner": {
                    "relation": RelationType.BelongsToOneRelation,
                    "modelClass": Person,
                    "join": {"from": "animals.ownerId", "to": "persons.id"},
                }
            }

        query = (
            Animal.query()
            .select("animals.name", "persons.name")
            .leftOuterJoinRelated("owner")
        )
        self.assertEqual(
            str(query),
            "SELECT animals.name, persons.name FROM animals LEFT OUTER JOIN persons ON animals.ownerId = persons.id",
        )

    def test_with_clause_basic(self):
        class Order(Model):
            tableName = "orders"

        large_orders = Order.query().select("id").where("amount", ">", 1000)
        query = QueryBuilder(Model).with_("large_orders", large_orders).select("*")
        self.assertEqual(
            str(query),
            "WITH large_orders AS (SELECT id FROM orders WHERE amount > 1000) SELECT *",
        )

    def test_with_clause_multiple_ctes(self):
        class Product(Model):
            tableName = "products"

        class Order(Model):
            tableName = "orders"

        large_orders = Order.query().select("id").where("amount", ">", 1000)
        expensive_products = Product.query().select("id").where("price", ">", 500)

        query = (
            QueryBuilder(Model)
            .with_("large_orders", large_orders)
            .with_("expensive_products", expensive_products)
            .select("*")
        )
        self.assertEqual(
            str(query),
            "WITH large_orders AS (SELECT id FROM orders WHERE amount > 1000), expensive_products AS (SELECT id FROM products WHERE price > 500) SELECT *",
        )

    def test_with_clause_with_subquery_in_select(self):
        class Order(Model):
            tableName = "orders"

        class Customer(Model):
            tableName = "customers"

        customer_orders_cte = (
            Order.query()
            .select("customer_id", QueryBuilder.raw("COUNT(id) as total_orders"))
            .groupBy("customer_id")
        )

        query = (
            Customer.query()
            .with_("customer_orders", customer_orders_cte)
            .select(
                "customers.id",
                "customers.name",
                QueryBuilder.raw(
                    "(SELECT total_orders FROM customer_orders WHERE customer_orders.customer_id = customers.id) AS order_count"
                ),
            )
        )
        self.assertEqual(
            str(query),
            "WITH customer_orders AS (SELECT customer_id, COUNT(id) as total_orders FROM orders GROUP BY customer_id) SELECT customers.id, customers.name, (SELECT total_orders FROM customer_orders WHERE customer_orders.customer_id = customers.id) AS order_count FROM customers",
        )

    def test_offset(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("*").offset(10)
        self.assertEqual(str(query), "SELECT * FROM users OFFSET 10")

    def test_multiple_offset_calls_raise_error(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("*")
        query.offset(10)
        with self.assertRaisesRegex(
            ValueError, "Offset can only be set once per query."
        ):
            query.offset(20)

    def test_offset_non_integer_value_raises_error(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("*")
        with self.assertRaisesRegex(TypeError, "Offset value must be an integer."):
            query.offset("abc")  # type: ignore # Intentionally passing wrong type for test

        with self.assertRaisesRegex(TypeError, "Offset value must be an integer."):
            query.offset(10.5)  # type: ignore # Intentionally passing wrong type for test

    def test_simple_union(self):
        class User(Model):
            tableName = "users"

        class Customer(Model):
            tableName = "customers"

        active_users = User.query().select("id", "name").where("active", "=", True)
        active_customers = (
            Customer.query().select("id", "name").where("active", "=", True)
        )

        query = active_users.union(active_customers)
        self.assertEqual(
            str(query),
            "(SELECT id, name FROM users WHERE active = True) UNION (SELECT id, name FROM customers WHERE active = True)",
        )

    def test_simple_union_all(self):
        class User(Model):
            tableName = "users"

        class Customer(Model):
            tableName = "customers"

        active_users = User.query().select("id", "name").where("active", "=", True)
        active_customers = (
            Customer.query().select("id", "name").where("active", "=", True)
        )

        query = active_users.unionAll(active_customers)
        self.assertEqual(
            str(query),
            "(SELECT id, name FROM users WHERE active = True) UNION ALL (SELECT id, name FROM customers WHERE active = True)",
        )

    def test_union_with_offset(self):
        class User(Model):
            tableName = "users"

        class Customer(Model):
            tableName = "customers"

        active_users = User.query().select("id", "name").where("active", "=", True)
        active_customers = (
            Customer.query().select("id", "name").where("active", "=", True)
        )

        query = active_users.union(active_customers).offset(10)
        self.assertEqual(
            str(query),
            "(SELECT id, name FROM users WHERE active = True) UNION (SELECT id, name FROM customers WHERE active = True) OFFSET 10",
        )

    def test_union_with_cte_hoisting(self):
        class User(Model):
            tableName = "users"

        class Customer(Model):
            tableName = "customers"

        class Region(Model):
            tableName = "regions"

        user_regions = Region.query().select("id").where("type", "=", "user")
        customer_regions = Region.query().select("id").where("type", "=", "customer")

        users = (
            User.query()
            .with_("user_regions", user_regions)
            .select("id")
            .join("user_regions", "users.region_id", "=", "user_regions.id")
        )
        customers = (
            Customer.query()
            .with_("customer_regions", customer_regions)
            .select("id")
            .join("customer_regions", "customers.region_id", "=", "customer_regions.id")
        )

        query = users.union(customers)

        self.assertEqual(
            str(query),
            "WITH user_regions AS (SELECT id FROM regions WHERE type = 'user'), customer_regions AS (SELECT id FROM regions WHERE type = 'customer') (SELECT id FROM users JOIN user_regions ON users.region_id = user_regions.id) UNION (SELECT id FROM customers JOIN customer_regions ON customers.region_id = customer_regions.id)",
        )

    def test_simple_limit(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("*").limit(10)
        self.assertEqual(str(query), "SELECT * FROM users LIMIT 10")

    def test_limit_with_offset(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("*").limit(10).offset(20)
        self.assertEqual(str(query), "SELECT * FROM users LIMIT 10 OFFSET 20")

    def test_limit_with_union(self):
        class User(Model):
            tableName = "users"

        class Customer(Model):
            tableName = "customers"

        users = User.query().select("id")
        customers = Customer.query().select("id")

        query = users.union(customers).limit(10)
        self.assertEqual(
            str(query),
            "(SELECT id FROM users) UNION (SELECT id FROM customers) LIMIT 10",
        )

    def test_multiple_limit_calls_raise_error(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("*")
        query.limit(10)
        with self.assertRaisesRegex(
            ValueError, "LIMIT can only be set once per query."
        ):
            query.limit(20)

    def test_limit_non_integer_value_raises_error(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("*")
        with self.assertRaisesRegex(TypeError, "LIMIT value must be an integer."):
            query.limit("abc")  # type: ignore

        with self.assertRaisesRegex(TypeError, "LIMIT value must be an integer."):
            query.limit(10.5)  # type: ignore

    def test_simple_top(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("*").top(10)
        self.assertEqual(str(query), "SELECT TOP 10 * FROM users")

    def test_top_with_offset(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("*").top(10).offset(20)
        self.assertEqual(str(query), "SELECT TOP 10 * FROM users OFFSET 20")

    def test_top_and_limit_raise_error(self):
        class User(Model):
            tableName = "users"

        query_1 = User.query().select("*").top(10)
        with self.assertRaisesRegex(ValueError, r"Cannot use limit\(\) with top\(\)\."):
            query_1.limit(10)

        query_2 = User.query().select("*").limit(10)
        with self.assertRaisesRegex(ValueError, "Cannot use top\(\) with limit\(\)."):
            query_2.top(10)


if __name__ == "__main__":
    unittest.main()
