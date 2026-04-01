import unittest
from typing import Dict

from src.sustained import Model, QueryBuilder, RelationType
from src.sustained.expressions import Column


class TestQueryBuilder(unittest.TestCase):
    def test_select_from(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("id", "name")
        self.assertEqual(str(query), "SELECT id, name FROM users")

    def test_distinct(self):
        class User(Model):
            tableName = "users"

        query = User.query().distinct().select("country")
        self.assertEqual(str(query), "SELECT DISTINCT country FROM users")

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

    def test_from_with_subquery(self):
        class Movie(Model):
            tableName = "movies"

        subquery = Movie.query().where("rating", ">", 8)
        query = Movie.query().from_(subquery, "top_movies").select("*")
        self.assertEqual(
            str(query),
            "SELECT * FROM (SELECT * FROM movies WHERE rating > 8) AS top_movies",
        )

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
        self.assertEqual(str(query), "SELECT * FROM users")

    def test_top_with_offset(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("*").top(10).offset(20)
        self.assertEqual(str(query), "SELECT * FROM users OFFSET 20")

    def test_top_and_limit_raise_error(self):
        class User(Model):
            tableName = "users"

        query_1 = User.query().select("*").top(10)
        with self.assertRaisesRegex(ValueError, r"Cannot use limit\(\) with top\(\)."):
            query_1.limit(10)

        query_2 = User.query().select("*").limit(10)
        with self.assertRaises(ValueError):
            query_2.top(10)

    def test_order_by_with_limit_and_offset(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("*").orderBy("name", "desc").limit(10).offset(5)
        self.assertEqual(
            str(query), "SELECT * FROM users ORDER BY name DESC LIMIT 10 OFFSET 5"
        )

    def test_order_by_with_union(self):
        class User(Model):
            tableName = "users"

        class Customer(Model):
            tableName = "customers"

        users = User.query().select("id", "name").where("status", "=", "active")
        customers = Customer.query().select("id", "name").where("status", "=", "active")

        query = users.union(customers).orderBy("name").limit(10)
        self.assertEqual(
            str(query),
            "(SELECT id, name FROM users WHERE status = 'active') UNION (SELECT id, name FROM customers WHERE status = 'active') ORDER BY name ASC LIMIT 10",
        )


class TestWhereClauseOperators(unittest.TestCase):
    def setUp(self):
        class User(Model):
            tableName = "users"

        self.User = User

    def test_where_like(self):
        query = self.User.query().whereLike("name", "John%")
        self.assertEqual(str(query), "SELECT * FROM users WHERE name LIKE 'John%'")

    def test_where_ilike(self):
        query = self.User.query().whereILike("name", "john%")
        self.assertEqual(str(query), "SELECT * FROM users WHERE name ILIKE 'john%'")

    def test_where_like_and_or(self):
        query = (
            self.User.query()
            .where("age", ">", 18)
            .andWhereLike("email", "%@example.com")
            .orWhereILike("name", "jane%")
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM users WHERE age > 18 AND email LIKE '%@example.com' OR name ILIKE 'jane%'",
        )

    def test_where_null(self):
        query = self.User.query().whereNull("deleted_at")
        self.assertEqual(str(query), "SELECT * FROM users WHERE deleted_at IS NULL")

    def test_where_not_null(self):
        query = self.User.query().whereNotNull("deleted_at")
        self.assertEqual(str(query), "SELECT * FROM users WHERE deleted_at IS NOT NULL")

    def test_where_null_and_or(self):
        query = (
            self.User.query()
            .where("status", "=", "active")
            .andWhereNull("avatar_url")
            .orWhereNotNull("updated_at")
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM users WHERE status = 'active' AND avatar_url IS NULL OR updated_at IS NOT NULL",
        )


class TestFluentSelects(unittest.TestCase):
    def setUp(self):
        class User(Model):
            tableName = "users"

        self.User = User

    def test_count_simple(self):
        query = self.User.query().count()
        self.assertEqual(str(query), "SELECT COUNT(*) FROM users")

    def test_count_with_alias(self):
        query = self.User.query().count("id", alias="total_users")
        self.assertEqual(str(query), "SELECT COUNT(id) AS total_users FROM users")

    def test_sum_simple(self):
        query = self.User.query().sum("karma")
        self.assertEqual(str(query), "SELECT SUM(karma) FROM users")

    def test_sum_with_alias(self):
        query = self.User.query().sum("karma", alias="total_karma")
        self.assertEqual(str(query), "SELECT SUM(karma) AS total_karma FROM users")

    def test_avg_simple(self):
        query = self.User.query().avg("age")
        self.assertEqual(str(query), "SELECT AVG(age) FROM users")

    def test_avg_with_alias(self):
        query = self.User.query().avg("age", alias="average_age")
        self.assertEqual(str(query), "SELECT AVG(age) AS average_age FROM users")

    def test_min_simple(self):
        query = self.User.query().min("karma")
        self.assertEqual(str(query), "SELECT MIN(karma) FROM users")

    def test_min_with_alias(self):
        query = self.User.query().min("karma", alias="lowest_karma")
        self.assertEqual(str(query), "SELECT MIN(karma) AS lowest_karma FROM users")

    def test_max_simple(self):
        query = self.User.query().max("karma")
        self.assertEqual(str(query), "SELECT MAX(karma) FROM users")

    def test_max_with_alias(self):
        query = self.User.query().max("karma", alias="highest_karma")
        self.assertEqual(str(query), "SELECT MAX(karma) AS highest_karma FROM users")

    def test_select_window(self):
        query = self.User.query().select_window(
            "ROW_NUMBER", "row_num", partition_by=["department"], order_by=["age DESC"]
        )
        self.assertEqual(
            str(query),
            "SELECT ROW_NUMBER() OVER (PARTITION BY department ORDER BY age DESC) AS row_num FROM users",
        )

    def test_select_case(self):
        query = self.User.query().select_case(
            "level",
            "Beginner",
            when_clauses=[
                ("karma > 1000", "Advanced"),
                ("karma > 500", "Intermediate"),
            ],
        )
        self.assertEqual(
            str(query),
            "SELECT CASE WHEN karma > 1000 THEN 'Advanced' WHEN karma > 500 THEN 'Intermediate' ELSE 'Beginner' END AS level FROM users",
        )

    def test_select_case_with_column(self):
        query = self.User.query().select_case(
            "status",
            Column("default_status"),
            when_clauses=[("is_active = 1", Column("active_status"))],
        )
        self.assertEqual(
            str(query),
            "SELECT CASE WHEN is_active = 1 THEN active_status ELSE default_status END AS status FROM users",
        )

    def test_mixed_fluent_and_regular_select(self):
        query = (
            self.User.query()
            .select("id", "name")
            .count("id", alias="id_count")
            .sum("karma", alias="total_karma")
        )
        self.assertEqual(
            str(query),
            "SELECT id, name, COUNT(id) AS id_count, SUM(karma) AS total_karma FROM users",
        )


if __name__ == "__main__":
    unittest.main()


class TestFuncRendering(unittest.TestCase):
    def setUp(self):
        class User(Model):
            tableName = "users"

        self.User = User

    def test_simple_func(self):
        query = self.User.query().select_func("COALESCE", Column("name"), "Unknown")
        self.assertEqual(str(query), "SELECT COALESCE(name, 'Unknown') FROM users")

    def test_func_with_alias(self):
        query = self.User.query().select_func(
            "LOWER", Column("username"), alias="lower_username"
        )
        self.assertEqual(
            str(query), "SELECT LOWER(username) AS lower_username FROM users"
        )

    def test_nested_func(self):
        from sustained.expressions import Func

        inner_func = Func("CONCAT", Column("first_name"), " ", Column("last_name"))
        query = self.User.query().select_func("UPPER", inner_func, alias="full_name")
        self.assertEqual(
            str(query),
            "SELECT UPPER(CONCAT(first_name, ' ', last_name)) AS full_name FROM users",
        )

    def test_func_with_mixed_args(self):
        from sustained.expressions import AggregateExpression

        agg = AggregateExpression("COUNT", "*")
        query = self.User.query().select_func("FORMAT", "User count: %s", agg)
        self.assertEqual(
            str(query), "SELECT FORMAT('User count: %s', COUNT(*)) FROM users"
        )
