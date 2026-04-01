# Sustained Developer Guide

This document provides a high-level overview of the architecture and design patterns used in the Sustained query builder. It is intended for developers who want to contribute to the package or understand its internal workings.

## Core Architecture

The design of Sustained is heavily inspired by [Objection.js](https://vincit.github.io/objection.js/), with a focus on a fluent, chainable API for building SQL queries programmatically. The query builder is designed to be **mutable**, meaning that each method call modifies the internal state of the current `QueryBuilder` instance and returns `self` to allow for chaining.

The architecture is composed of several key components that work together to translate a series of method calls into a final SQL string.

### The `Model`

The `Model` class (`sustained/model.py`) is the primary entry point for users of the library. Its main responsibilities are:
-   Holding metadata about a database table (name, schema, relations).
-   Storing the dialect configuration for queries generated from it (`Dialects.DEFAULT` by default).
-   Acting as a factory for creating new `QueryBuilder` instances via the `.query()` class method.

### The `QueryBuilder`

The `QueryBuilder` (`sustained/builder.py`) is the central component of the library. It acts as the main fluent interface that users interact with.

-   **State Management:** It does not manage the complex state of the query directly. Instead, it holds instances of several specialized `*ClauseBuilder` objects.
-   **Composition:** When a method like `.where()` or `.select()` is called on the `QueryBuilder`, it delegates that call to the appropriate internal builder (e.g., `self._where_builder` or `self._select_clause_builder`).
-   **Assembly:** When `str(query)` is finally called, the `QueryBuilder` is responsible for assembling the final SQL string by calling `str()` on each of its internal builders in the correct order and passing the result through the `Compiler`.

### The `*ClauseBuilder`s

Located in `sustained/builders/`, these classes (`WhereClauseBuilder`, `JoinClauseBuilder`, etc.) are each responsible for managing the state of a single, specific clause of a SQL query.

For example, the `WhereClauseBuilder` maintains a list of `WHERE` conditions, handles the logic for grouping them with `AND` or `OR`, and its `__str__` method renders the final `WHERE ...` SQL fragment.

### The `Compiler`

The `Compiler` (`sustained/compilers/`) is responsible for translating the query representation into a dialect-specific SQL string.

-   **Dialect-Specific Syntax:** Each dialect (`PostgreSQL`, `MSSQL`, etc.) has its own `Compiler` subclass that can override base methods to implement specific syntax (e.g., `TOP` vs. `LIMIT`, identifier quoting).
-   **Rendering:** The `QueryBuilder` uses the configured compiler to render parts of the query that vary between dialects, such as `LIMIT`/`OFFSET` clauses and function calls.

### Expression Classes

Located in `sustained/expressions.py`, classes like `Func`, `Column`, and `AggregateExpression` are simple data structures. They represent parts of a query that are not simple literal values. They are passed from the `QueryBuilder` down through the `*ClauseBuilder`s and are ultimately rendered into SQL by the `Compiler`.

## The Query Building Lifecycle

Understanding the lifecycle of a query is key to understanding the architecture.

1.  **Instantiation:** A user calls `MyModel.query()`. The `Model` creates a `QueryBuilder` instance, passing it the currently configured `Dialect`.
2.  **Construction:** The user chains methods like `.select()`, `.where()`, and `.orderBy()`. Each of these calls is delegated to the corresponding internal `*ClauseBuilder`, which updates its internal state.
3.  **Compilation:** The user calls `str(query_builder)` to get the final SQL string.
4.  **Assembly:** The `QueryBuilder.__str__()` method is invoked. It calls `str()` on each of its internal builders (`_select_clause_builder`, `_where_builder`, etc.) to get their rendered SQL fragments.
5.  **Dialect-Specific Rendering:** For parts of the query that are dialect-dependent (like `LIMIT`/`OFFSET`), the `QueryBuilder` calls methods on its configured `Compiler` instance (e.g., `self._compiler.compile_limit_offset(...)`).
6.  **Final String:** The `QueryBuilder` joins all the rendered fragments together into the final, complete SQL string.

## Extending the ORM

### Adding a New Dialect

1.  Create a new compiler class in `sustained/compilers/` that inherits from `sustained.compilers.base.Compiler`.
2.  Override any methods needed to implement dialect-specific syntax (e.g., `compile_limit_offset` for `TOP` vs `LIMIT`, `quote_identifier` for `[]` vs `""`).
3.  Register the new compiler by adding it to the `Dialects` enum in `sustained/dialects.py`.

### Adding a New SQL Function

1.  Open `sustained/functions.py`.
2.  Add the new function's name and its supported dialects to the `FunctionRegistry`.
    ```python
    self.register(
        "MY_FUNCTION",
        FunctionMetadata(supported_dialects=[Dialects.POSTGRES, Dialects.MSSQL])
    )
    ```
3.  That's it. The `QueryBuilder.select_func()` method will now automatically validate the new function against the active dialect. If the function requires special rendering syntax for a specific dialect, you can add a custom renderer.

## Dynamic Method Resolution with `__getattr__`

The `QueryBuilder` uses a `__getattr__` method to provide a wide, expressive API without having to explicitly define dozens of similar methods. This is how it supports variations like `where`, `orWhere`, `whereIn`, `andWhereLike`, etc.

When a method is called on a `QueryBuilder` instance that doesn't actually exist (e.g., `orWhereIn(...)`), `__getattr__` intercepts the call. It uses regular expressions to determine if the method name matches a known pattern (e.g., `^(or|and)?(WhereIn)$`). If it finds a match, it dynamically calls the corresponding method on the appropriate internal builder (`_where_builder` in this case), passing along the arguments.

This use of metaprogramming makes the fluent interface possible.
