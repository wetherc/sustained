# Sustained Documentation

Welcome to the documentation for Sustained, a Python query builder inspired by [Objection.js](https://vincit.github.io/objection.js/).

This documentation provides a detailed guide on how to use the library to define models and build complex SQL queries in a programmatic way.

## Getting Started

If you are new to Sustained, it's recommended to read the guides in the following order:

1.  **[Models](./models):** Learn how to define models that map to your database tables.
2.  **[Queries](./queries):** Understand how to start queries and select data.
3.  **[Filtering](./filtering):** Dive into the various `where` methods for filtering your results.
4.  **[Relations and Joins](./relations):** Learn how to define relationships between models and join them in your queries.

## API Reference

The docstrings in the source code provide a comprehensive API reference. You can use Python's built-in `help()` function to get detailed information about any class or method.

```python
from sustained import Model, QueryBuilder

# Get help on the Model class
help(Model)

# Get help on the QueryBuilder class
help(QueryBuilder)
```
