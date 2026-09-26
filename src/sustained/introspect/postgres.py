"""
The Postgres read: information_schema for columns, and pg_catalog for
indexes, foreign keys, checks, comments, and enum types.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple, cast

from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    SchemaPlan,
    Snapshot,
)
from sustained.introspect.scope import (
    _declared_schema,
    _is_generated_not_null_check,
    _one_schema_per_table,
    _row_text,
    _scoped_filter,
)
from sustained.types import RowValue


def _postgres_column_type(
    data_type: str,
    udt_name: Optional[str],
    char_length: Optional[RowValue],
    precision: Optional[RowValue],
    scale: Optional[RowValue],
) -> str:
    """
    The type spelling a Postgres column is compared on. information_schema's
    data_type alone loses too much: a varchar drops its length, a numeric
    its precision, and an enum reads as USER-DEFINED. The parameters go
    back on, and an enum reports its type's own name from udt_name.
    """
    if data_type == "USER-DEFINED" and udt_name:
        return str(udt_name)
    if data_type in ("character varying", "character") and char_length is not None:
        return f"{data_type}({char_length})"
    if data_type == "numeric" and precision is not None:
        return f"numeric({precision},{scale if scale is not None else 0})"
    return data_type


# How pg_constraint spells a referential action, mapped to the words
# the information_schema views and the model declarations use.
_PG_FK_ACTIONS = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def _pg_fk_action(code: Optional[RowValue]) -> Optional[str]:
    """The action a pg_constraint action character stands for."""
    if code is None:
        return None
    return _PG_FK_ACTIONS.get(str(code), str(code).upper())


def _postgres_plan(schemas: Tuple[str, ...] = ()) -> SchemaPlan:
    # Everything below keys on the bare table name, so an unscoped read
    # merges app.users into public.users and the diff never converges.
    # The read covers the schema the connection is on, plus every schema
    # the models declare.
    # current_schema() returns NULL when the first search_path entry
    # names a schema that does not exist. _scoped_filter compares it
    # beside the declared schemas, which still match in that case.
    table_filter = _scoped_filter("c.table_schema", "current_schema()", schemas)
    namespace_filter = _scoped_filter("n.nspname", "current_schema()", schemas)
    columns_by_table: Dict[str, Dict[str, IntrospectedColumn]] = {}
    spelled_tables: Dict[str, str] = {}
    table_schemas: Dict[str, str] = {}
    column_rows = yield (
        "SELECT c.table_name, c.column_name, c.data_type, c.udt_name, "
        "c.character_maximum_length, c.numeric_precision, c.numeric_scale, "
        "c.is_nullable, c.column_default, c.table_schema "
        "FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        f"WHERE {table_filter} "
        "AND t.table_type = 'BASE TABLE' "
        "ORDER BY c.table_name, c.ordinal_position"
    )
    schema_of_table: Dict[str, str] = {}
    for row in column_rows:
        table, name, data_type, udt_name = (str(v) for v in row[:4])
        char_length, precision, scale, is_nullable, default = row[4:9]
        if len(row) > 9 and row[9] is not None:
            _one_schema_per_table(schema_of_table, table.lower(), str(row[9]))
            declared_schema = _declared_schema(schemas, row[9])
            if declared_schema is not None:
                table_schemas[table.lower()] = declared_schema
        spelled_tables.setdefault(table.lower(), table)
        columns_by_table.setdefault(table.lower(), {})[name.lower()] = (
            IntrospectedColumn(
                raw_type=_postgres_column_type(
                    data_type, udt_name, char_length, precision, scale
                ),
                nullable=str(is_nullable).upper() == "YES",
                primary_key=False,
                default=None if default is None else str(default),
                name=name,
            )
        )

    primary_keys: Dict[str, Tuple[str, ...]] = {}
    indexes: Dict[str, Dict[str, IntrospectedIndex]] = {}
    try:
        index_rows = yield (
            "SELECT t.relname, i.relname, ix.indisunique, ix.indisprimary, "
            "a.attname, EXISTS (SELECT 1 FROM pg_catalog.pg_constraint pc "
            "WHERE pc.conindid = ix.indexrelid AND pc.contype = 'u') "
            "FROM pg_catalog.pg_index ix "
            "JOIN pg_catalog.pg_class t ON t.oid = ix.indrelid "
            "JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
            "CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) "
            "LEFT JOIN pg_catalog.pg_attribute a "
            "ON a.attrelid = t.oid AND a.attnum = k.attnum "
            "WHERE t.relkind IN ('r', 'p') "
            f"AND {namespace_filter} "
            "ORDER BY t.relname, i.relname, k.ord"
        )
        index_columns: Dict[Tuple[str, str, bool, bool, bool], List[Optional[str]]] = {}
        spelled_indexes: Dict[Tuple[str, str], str] = {}
        for table, index, unique, primary, attname, backs in index_rows:
            spelled_indexes[(str(table).lower(), str(index).lower())] = str(index)
            key = (
                str(table).lower(),
                str(index).lower(),
                bool(unique),
                bool(primary),
                bool(backs),
            )
            index_columns.setdefault(key, []).append(
                None if attname is None else str(attname).lower()
            )
        for (table, index, unique, primary, backs), names in index_columns.items():
            if any(name is None for name in names):
                # An expression index has no column name for that key part.
                # It cannot be compared against a model's column list, so it
                # is left out of the schema rather than crashing the read.
                continue
            key_columns = tuple(cast(str, name) for name in names)
            if primary:
                primary_keys[table] = key_columns
            else:
                indexes.setdefault(table, {})[index] = IntrospectedIndex(
                    key_columns,
                    unique,
                    constraint=backs,
                    name=spelled_indexes[(table, index)],
                )
    except Exception:
        # No pg_index to read; degrade to columns without keys or indexes.
        pass

    foreign_keys: Dict[str, Dict[str, IntrospectedForeignKey]] = {}
    constraints_read = False
    try:
        # pg_constraint is read instead of the referential_constraints
        # view because a constraint name is unique per table, not per
        # schema. The view joins on the name alone, so two tables in one
        # schema with a same-named key cross-multiply into garbled
        # column lists. conrelid tells the two apart for free.
        fk_rows = yield (
            "SELECT src.relname, con.conname, sa.attname, tgt.relname, "
            "ta.attname, con.confdeltype, con.confupdtype, "
            "NULLIF(tn.nspname, current_schema()) "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class src ON src.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = src.relnamespace "
            "JOIN pg_catalog.pg_class tgt ON tgt.oid = con.confrelid "
            "JOIN pg_catalog.pg_namespace tn ON tn.oid = tgt.relnamespace "
            "CROSS JOIN LATERAL unnest(con.conkey, con.confkey) "
            "WITH ORDINALITY AS k(attnum, refattnum, ord) "
            "JOIN pg_catalog.pg_attribute sa "
            "ON sa.attrelid = con.conrelid AND sa.attnum = k.attnum "
            "JOIN pg_catalog.pg_attribute ta "
            "ON ta.attrelid = con.confrelid AND ta.attnum = k.refattnum "
            "WHERE con.contype = 'f' "
            f"AND {namespace_filter} "
            "ORDER BY src.relname, con.conname, k.ord"
        )
        fk_parts: Dict[Tuple[str, str], List[Sequence[RowValue]]] = {}
        for row in fk_rows:
            part_key = (str(row[0]).lower(), str(row[1]).lower())
            fk_parts.setdefault(part_key, []).append(row)
        for (table, cname), rows in fk_parts.items():
            first = rows[0]
            foreign_keys.setdefault(table, {})[cname] = IntrospectedForeignKey(
                columns=tuple(str(r[2]).lower() for r in rows),
                target_table=str(first[3]).lower(),
                target_columns=tuple(str(r[4]).lower() for r in rows),
                on_delete=_pg_fk_action(first[5]),
                on_update=_pg_fk_action(first[6]),
                name=str(first[1]),
                target_schema=_row_text(first, 7),
            )
        constraints_read = True
    except Exception:
        # No pg_constraint to read; degrade to no foreign keys.
        pass

    checks: Dict[str, Dict[str, str]] = {}
    check_names: Dict[str, Dict[str, str]] = {}
    checks_read = False
    try:
        # The check_constraints view joins on the schema and the name,
        # but a check name is unique per table only, so a table could
        # read another table's expression. pg_constraint keys each check
        # on conrelid. The substring drops the "CHECK " prefix that
        # pg_get_constraintdef() writes, as the view does.
        check_rows = yield (
            "SELECT src.relname, con.conname, "
            "substring(pg_get_constraintdef(con.oid) from 7) "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class src ON src.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = src.relnamespace "
            "WHERE con.contype = 'c' "
            f"AND {namespace_filter}"
        )
        for table, cname, clause in check_rows:
            name = str(cname).lower()
            expression = str(clause)
            if _is_generated_not_null_check(name, expression):
                continue
            checks.setdefault(str(table).lower(), {})[name] = expression
            check_names.setdefault(str(table).lower(), {})[name] = str(cname)
        checks_read = True
    except Exception:
        # No pg_constraint to read; degrade to no checks.
        pass

    comments: Dict[str, Dict[str, str]] = {}
    comments_read = False
    try:
        comment_rows = yield (
            "SELECT c.relname, a.attname, d.description "
            "FROM pg_catalog.pg_description d "
            "JOIN pg_catalog.pg_class c ON c.oid = d.objoid "
            "JOIN pg_catalog.pg_attribute a "
            "ON a.attrelid = d.objoid AND a.attnum = d.objsubid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE d.objsubid > 0 "
            f"AND {namespace_filter}"
        )
        for table, name, description in comment_rows:
            comments.setdefault(str(table).lower(), {})[str(name).lower()] = str(
                description
            )
        comments_read = True
    except Exception:
        # No pg_description to read; degrade to no comments.
        pass

    enum_types: Dict[str, Tuple[str, ...]] = {}
    enum_types_read = False
    try:
        enum_rows = yield (
            "SELECT t.typname, e.enumlabel "
            "FROM pg_catalog.pg_type t "
            "JOIN pg_catalog.pg_enum e ON e.enumtypid = t.oid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace "
            f"WHERE {namespace_filter} "
            "ORDER BY t.typname, e.enumsortorder"
        )
        values_by_type: Dict[str, List[str]] = {}
        for typname, label in enum_rows:
            values_by_type.setdefault(str(typname).lower(), []).append(str(label))
        enum_types = {name: tuple(vals) for name, vals in values_by_type.items()}
        enum_types_read = True
    except Exception:
        # No pg_enum to read; degrade to no enum types.
        pass

    schema = Snapshot(
        enum_types=enum_types,
        enum_types_read=enum_types_read,
        constraints_read=constraints_read,
        checks_read=checks_read,
        comments_read=comments_read,
    )
    for table, columns in columns_by_table.items():
        pk = primary_keys.get(table, ())
        for pk_col in pk:
            if pk_col in columns:
                columns[pk_col] = columns[pk_col]._replace(primary_key=True)
        for name, column in columns.items():
            values = enum_types.get(column.raw_type.lower())
            if values is not None:
                columns[name] = column._replace(
                    enum_name=column.raw_type.lower(), enum_values=values
                )
        for name, comment in comments.get(table, {}).items():
            if name in columns:
                columns[name] = columns[name]._replace(comment=comment)
        schema[table] = IntrospectedTable(
            columns=columns,
            primary_key=pk,
            foreign_keys=foreign_keys.get(table, {}),
            indexes=indexes.get(table, {}),
            checks=checks.get(table, {}),
            name=spelled_tables.get(table),
            check_names=check_names.get(table, {}),
            schema=table_schemas.get(table),
        )
    return schema
