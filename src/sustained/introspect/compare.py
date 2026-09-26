"""
Comparing two schema reads. A rehearsal uses it to check that the down
steps put the schema back where it started.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from sustained.introspect.model import IntrospectedColumn, IntrospectedTable
from sustained.introspect.normalize import normalize_type, type_params


def _column_shape(column: IntrospectedColumn) -> Tuple[str, Optional[str], bool, bool]:
    """
    One column reduced to the parts two snapshots are compared on: the
    logical type, its parameters, nullability, and key membership. The
    default is left out, since engines report a default they generated
    themselves in spellings that differ between an original column and a
    rebuilt one.
    """
    return (
        normalize_type(column.raw_type),
        type_params(column.raw_type),
        column.nullable,
        column.primary_key,
    )


def _describe_column(column: IntrospectedColumn) -> str:
    """A column shape in one readable phrase."""
    text = (column.raw_type or "?").upper()
    if not column.nullable:
        text += " NOT NULL"
    if column.primary_key:
        text += " PRIMARY KEY"
    return text


def diff_snapshots(
    before: Dict[str, IntrospectedTable], after: Dict[str, IntrospectedTable]
) -> List[str]:
    """
    Compares two introspected schemas and returns one line per difference,
    empty when they match. A rehearsal uses it to check that the down
    steps put the schema back where it started: `before` is the snapshot
    taken first, `after` is the schema once the down steps have run.

    Tables and columns are compared. Indexes, constraints, defaults, and
    comments are not: their reported spellings differ enough between
    engines that a difference here would report noise as a failure.
    """
    lines: List[str] = []
    for table in sorted(set(after) - set(before)):
        lines.append(f"table '{table}' left behind")
    for table in sorted(set(before) - set(after)):
        lines.append(f"table '{table}' missing")
    for table in sorted(set(before) & set(after)):
        old, new = before[table].columns, after[table].columns
        for column in sorted(set(new) - set(old)):
            lines.append(f"column '{table}.{column}' left behind")
        for column in sorted(set(old) - set(new)):
            lines.append(f"column '{table}.{column}' missing")
        for column in sorted(set(old) & set(new)):
            if _column_shape(old[column]) != _column_shape(new[column]):
                lines.append(
                    f"column '{table}.{column}' changed: "
                    f"{_describe_column(old[column])} became "
                    f"{_describe_column(new[column])}"
                )
    return lines
