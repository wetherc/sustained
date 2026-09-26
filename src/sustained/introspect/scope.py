"""
Holding a catalog read to the schemas it covers, and the row helpers
the catalog reads share.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from sustained.types import RowValue


def _schema_literal(name: str) -> str:
    """One schema name as a SQL string literal."""
    return "'{}'".format(name.replace("'", "''"))


def _schema_predicate(
    column: str, current_sql: Optional[str], schemas: Tuple[str, ...]
) -> Optional[str]:
    """
    The WHERE fragment that holds `column` to the schemas a read covers:
    the connection's own schema when the engine can name it, plus every
    schema the models declare. None when the engine has no expression for
    the current schema and the caller named none, which leaves the read
    unscoped.

    The declared schemas make their own IN list and the current schema
    is compared beside it with OR. Postgres returns NULL from
    current_schema() when the first search_path entry names a schema that
    does not exist, and then the OR branch matches no rows while the
    declared schemas still match.
    """
    parts: List[str] = []
    if schemas:
        literals: List[str] = []
        for name in schemas:
            literal = _schema_literal(name)
            if literal not in literals:
                literals.append(literal)
        parts.append("{} IN ({})".format(column, ", ".join(literals)))
    if current_sql is not None:
        parts.append(f"{column} = {current_sql}")
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return "({})".format(" OR ".join(parts))


def _scoped_filter(column: str, current_sql: str, schemas: Tuple[str, ...]) -> str:
    """
    _schema_predicate() for an engine that can name the schema the
    connection is on, where the predicate is never empty.
    """
    predicate = _schema_predicate(column, current_sql, schemas)
    assert predicate is not None
    return predicate


def _one_schema_per_table(seen: Dict[str, str], table: str, schema: str) -> None:
    """
    Records the schema a table came from, and refuses a second one.

    A snapshot keys on the bare table name. A read covers the schema the
    connection is on plus every schema the models declare, so it can
    return two tables of one name. Their columns would merge into one
    entry, and the merged table matches no model: the diff would report
    columns to add and to drop that are not there. Reading one schema at
    a time is the way out.
    """
    first = seen.setdefault(table, schema)
    if first.lower() != schema.lower():
        raise ValueError(
            f"The schemas '{first}' and '{schema}' both hold a table named "
            f"'{table}'. A schema read keys on the bare table name, so the "
            "two cannot be told apart. Take the declared tableSchema off "
            "the models, or rename one of the tables. A read covers the "
            "schema the connection is on as well as the declared ones, so "
            "it cannot be narrowed past that."
        )


def _declared_schema(schemas: Tuple[str, ...], value: RowValue) -> Optional[str]:
    """
    The schema a row names, when it is one the models declare, or None.
    A table in the connection's own schema keeps None, so a statement
    names it the way the models do.
    """
    if value is None:
        return None
    declared = {name.lower() for name in schemas}
    return str(value) if str(value).lower() in declared else None


def _row_text(row: Sequence[RowValue], index: int) -> Optional[str]:
    """The text a row has at `index`, or None when it has none there."""
    if len(row) <= index or row[index] is None:
        return None
    return str(row[index])


def _is_generated_not_null_check(name: str, expression: str) -> bool:
    """
    Whether a check row is the constraint an engine writes for a NOT NULL
    column. Postgres and DuckDB both report one, named after the column
    with a _not_null suffix. It belongs to the column's own nullable
    flag, not to the table's checks, and a model never declares it.
    """
    return name.endswith("_not_null") and "IS NOT NULL" in expression.upper()
