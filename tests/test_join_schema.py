"""
Relation joins name the related table the way the related model does, with
its tableSchema and database in front.
"""

import unittest

from sustained import Model, RelationType
from sustained.dialects import Dialects


class Order(Model):
    tableName = "orders"
    tableSchema = "sales"
    _dialect = Dialects.POSTGRES


class Tag(Model):
    tableName = "tags"
    database = "shop"
    tableSchema = "dbo"
    _dialect = Dialects.POSTGRES


class OrderTag(Model):
    tableName = "order_tags"
    tableSchema = "sales"
    _dialect = Dialects.POSTGRES


class Customer(Model):
    tableName = "customers"
    _dialect = Dialects.POSTGRES
    relationMappings = {
        "orders": {
            "relation": RelationType.HasManyRelation,
            "modelClass": Order,
            "join": {"from": "customers.id", "to": "orders.customer_id"},
        },
        "qualified_orders": {
            "relation": RelationType.HasManyRelation,
            "modelClass": Order,
            "join": {"from": "customers.id", "to": "sales.orders.customer_id"},
        },
    }


class SalesOrder(Model):
    tableName = "orders"
    tableSchema = "sales"
    _dialect = Dialects.POSTGRES
    relationMappings = {
        "tags": {
            "relation": RelationType.ManyToManyRelation,
            "modelClass": Tag,
            "join": {
                "from": "sales.orders.id",
                "through": {
                    "from": {"table": OrderTag, "key": "order_id"},
                    "to": {"table": OrderTag, "key": "tag_id"},
                },
                "to": "shop.dbo.tags.id",
            },
        },
    }


class TestJoinSchema(unittest.TestCase):
    def test_basic_join_names_the_schema(self):
        self.assertEqual(
            str(Customer.query().innerJoinRelated("orders")),
            'SELECT * FROM "customers" INNER JOIN "sales"."orders" '
            'ON "customers"."id" = "orders"."customer_id"',
        )

    def test_alias_rewrites_a_bare_to_reference(self):
        self.assertEqual(
            str(Customer.query().innerJoinRelated("orders", alias="o")),
            'SELECT * FROM "customers" INNER JOIN "sales"."orders" AS "o" '
            'ON "customers"."id" = "o"."customer_id"',
        )

    def test_alias_rewrites_a_qualified_to_reference(self):
        self.assertEqual(
            str(Customer.query().innerJoinRelated("qualified_orders", alias="o")),
            'SELECT * FROM "customers" INNER JOIN "sales"."orders" AS "o" '
            'ON "customers"."id" = "o"."customer_id"',
        )

    def test_through_join_names_every_schema(self):
        self.assertEqual(
            str(SalesOrder.query().innerJoinRelated("tags", alias="t")),
            'SELECT * FROM "sales"."orders" '
            'INNER JOIN "sales"."order_tags" '
            'ON "sales"."orders"."id" = "sales"."order_tags"."order_id" '
            'INNER JOIN "shop"."dbo"."tags" AS "t" '
            'ON "sales"."order_tags"."tag_id" = "t"."id"',
        )


if __name__ == "__main__":
    unittest.main()
