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


if __name__ == "__main__":
    unittest.main()
