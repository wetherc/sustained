"""
The SQL Server read: information_schema, plus sys.indexes for plain
indexes and sys.foreign_keys for where each key points.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from sustained.introspect.information_schema import (
    MSSQL_CATALOG,
    _catalog_filter,
    _information_schema_plan,
    _merge_plain_indexes,
    _replace_foreign_keys,
)
from sustained.introspect.model import IntrospectedIndex, SchemaPlan


def _mssql_plan(schemas: Tuple[str, ...] = ()) -> SchemaPlan:
    """
    information_schema plus sys.indexes. The shared read sees unique
    constraints only; plain indexes and CREATE UNIQUE INDEX indexes live
    in sys.indexes. Without them, a model's declared index reads as
    missing on every plan, and the second run fails creating it again.
    """
    schema = yield from _information_schema_plan(MSSQL_CATALOG, schemas)
    index_filter = _catalog_filter(MSSQL_CATALOG, "SCHEMA_NAME(t.schema_id)", schemas)
    try:
        index_rows = yield (
            "SELECT t.name, i.name, i.is_unique, c.name "
            "FROM sys.indexes i "
            "JOIN sys.tables t ON t.object_id = i.object_id "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id "
            "AND ic.index_id = i.index_id "
            "JOIN sys.columns c ON c.object_id = ic.object_id "
            "AND c.column_id = ic.column_id "
            "WHERE i.is_primary_key = 0 AND i.is_unique_constraint = 0 "
            "AND i.name IS NOT NULL AND ic.is_included_column = 0 "
            f"AND {index_filter} "
            "ORDER BY t.name, i.name, ic.key_ordinal"
        )
        parts: Dict[Tuple[str, str, bool], List[str]] = {}
        spelled: Dict[str, str] = {}
        for table, name, is_unique, column in index_rows:
            key = (str(table).lower(), str(name).lower(), bool(is_unique))
            parts.setdefault(key, []).append(str(column).lower())
            spelled.setdefault(str(name).lower(), str(name))
        plain: Dict[str, Dict[str, IntrospectedIndex]] = {}
        for (table, name, unique), columns in parts.items():
            plain.setdefault(table, {})[name] = IntrospectedIndex(
                tuple(columns), unique, name=spelled[name]
            )
        _merge_plain_indexes(schema, plain)
    except Exception:
        # No sys views to read; keep the constraint-derived indexes.
        pass
    try:
        fk_rows = yield (
            "SELECT t.name, fk.name, pc.name, rt.name, rc.name, "
            "fk.delete_referential_action_desc, "
            "fk.update_referential_action_desc, "
            "NULLIF(SCHEMA_NAME(rt.schema_id), SCHEMA_NAME()) "
            "FROM sys.foreign_keys fk "
            "JOIN sys.tables t ON t.object_id = fk.parent_object_id "
            "JOIN sys.foreign_key_columns fkc "
            "ON fkc.constraint_object_id = fk.object_id "
            "JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id "
            "AND pc.column_id = fkc.parent_column_id "
            "JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id "
            "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id "
            "AND rc.column_id = fkc.referenced_column_id "
            f"WHERE {index_filter} "
            "ORDER BY t.name, fk.name, fkc.constraint_column_id"
        )
        _replace_foreign_keys(schema, fk_rows)
    except Exception:
        # No sys views to read; keep the keys without their targets.
        pass
    return schema
