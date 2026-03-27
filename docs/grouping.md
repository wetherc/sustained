---
layout: default
title: Grouping and Having Clauses
---



## Basic `groupBy` Methods

The `groupBy` method is used to specify one or more columns by which the query results should be grouped. This is a fundamental operation for aggregate functions.

```python
from my_project import Order

# Group by a single column
#   SELECT ...
#   GROUP BY customer_id
query = Order.query().groupBy("customer_id")
print(query)

# Group by multiple columns
#   SELECT ...
#   GROUP BY customer_id, order_status
query = Order.query().groupBy("customer_id", "order_status")
print(query)
```

## Basic `having` Methods

The `having`, `andHaving`, and `orHaving` methods are used to filter results after aggregation. They function similarly to `where` clauses but operate on grouped data, often involving aggregate functions.

A `having` clause takes three arguments: a column (often an aggregate function), an operator, and a value.

```python
# SELECT ...
# GROUP BY customer_id
# HAVING COUNT(id) > 10
query = Order.query().groupBy("customer_id").having("COUNT(id)", ">", 10)
print(query)

# Chain multiple `having` clauses
#   SELECT ...
#   GROUP BY customer_id
#   HAVING COUNT(id) > 5
#      AND SUM(total_amount) > 100
query = Order.query()
    .groupBy("customer_id")
    .having("COUNT(id)", ">", 5)
    .andHaving("SUM(total_amount)", ">", 100)
print(query)

# Use `orHaving` to add an alternative condition
#   SELECT ...
#   GROUP BY customer_id
#   HAVING AVG(item_price) < 50
#       OR MAX(quantity) > 10
query = Order.query()
    .groupBy("customer_id")
    .having("AVG(item_price)", "<", 50)
    .orHaving("MAX(quantity)", ">", 10)
print(query)
```
> **Note:** The first `having` call in a chain cannot be an `orHaving` or `andHaving`. It must be a plain `having`.

## `havingIn` and `havingNotIn`

To filter grouped results against a list of values, use the `havingIn` and `havingNotIn` methods.

### `havingIn`

This generates a `HAVING col IN (...)` clause, useful for checking if an aggregated value falls within a specific set.

```python
# SELECT ...
# GROUP BY order_status
# HAVING order_status IN ('completed', 'shipped')
query = (
    Order
        .query()
        .groupBy("order_status")
        .havingIn("order_status", ["completed", "shipped"]
)
print(query)

# You can also use `andHavingIn` and `orHavingIn`
#   SELECT ...
#   GROUP BY product_category
#   HAVING SUM(sales_count) > 100
#     AND product_category IN ('Electronics', 'Books')
query = Order.query()
    .groupBy("product_category")
    .having("SUM(sales_count)", ">", 100)
    .andHavingIn("product_category", ["Electronics", "Books"])
print(query)
```

### `havingNotIn`

This generates a `HAVING col NOT IN (...)` clause, to exclude grouped results where an aggregated value is in a specific set.

```python
# SELECT ...
# GROUP BY region
# HAVING region NOT IN ('East', 'West')
query = Order.query().groupBy("region").havingNotIn("region", ["East", "West"])
print(query)
```

## `havingLike` and `havingILike`

To filter groups using `LIKE` and case-insensitive `ILIKE`, you can use the `havingLike` and `havingILike` methods.

### `havingLike`

This generates a `HAVING col LIKE 'pattern'` clause.

```python
# SELECT ... GROUP BY category HAVING category LIKE 'Sci-%'
Order.query().groupBy("category").havingLike("category", "Sci-%")
```

### `havingILike`

This generates a `HAVING col ILIKE 'pattern'` clause (for case-insensitive matching).

```python
# SELECT ... GROUP BY category HAVING category ILIKE 'sci-%'
Order.query().groupBy("category").havingILike("category", "sci-%")
```

## `havingNull` and `havingNotNull`

To check for `NULL` or `NOT NULL` values in grouped data, use `havingNull` and `havingNotNull`.

### `havingNull`

This generates a `HAVING col IS NULL` clause.

```python
# SELECT ... GROUP BY manager HAVING manager IS NULL
Order.query().groupBy("manager").havingNull("manager")
```

### `havingNotNull`

This generates a `HAVING col IS NOT NULL` clause.

```python
# SELECT ... GROUP BY manager HAVING manager IS NOT NULL
Order.query().groupBy("manager").havingNotNull("manager")
```

## Grouped `having` Clauses

For complex logical groupings within your `HAVING` clause, you can pass a callable (like a lambda function) to any `having` method. The function will receive a temporary `HavingClauseBuilder` instance, allowing you to build nested conditions that are wrapped in parentheses.

This is useful for creating conditions like `... AND (condition A OR condition B)`.

```python
# SELECT ...
# GROUP BY customer_id
# HAVING COUNT(id) > 5
#    AND (SUM(total_amount) < 100 OR MAX(quantity) <= 2)

query = Order.query()
    .groupBy("customer_id")
    .having("COUNT(id)", ">", 5)
    .andHaving(lambda q: (
        q.having("SUM(total_amount)", "<", 100)
         .orHaving("MAX(quantity)", "<=", 2)
    ))
print(query)
```

### Complex Grouping

You can nest these groups as deeply as needed to construct intricate `HAVING` logic.

```python
# SELECT ...
# GROUP BY product_type
# HAVING total_revenue > 10000
#   AND (
#     (average_rating > 4.0 AND product_type = 'premium') OR
#     (inventory_count < 10 AND product_type = 'clearance')
#   )

query = Order.query()
    .groupBy("product_type")
    .having("total_revenue", ">", 10000)
    .andHaving(lambda q: (
        q.having(lambda group1: (
            group1.having("average_rating", ">", 4.0)
                  .andHaving("product_type", "=", "premium")
        )).orHaving(lambda group2: (
            group2.having("inventory_count", "<", 10)
                  .andHaving("product_type", "=", "clearance")
        ))
    ))
print(query)
```
