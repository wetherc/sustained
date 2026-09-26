"""
The constraint half of the schema diff: pairing declared foreign keys
and checks with the ones the catalog reports, by name or by content,
and recording what differs.
"""

from __future__ import annotations

import re
from typing import (
    TYPE_CHECKING,
    Callable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from sustained.autogenerate.diff import SchemaDiff, _constraints_fixed_at_create
from sustained.introspect import (
    IntrospectedForeignKey,
    IntrospectedTable,
    Snapshot,
    is_sequence_default,
    normalize_check,
    normalize_default,
)
from sustained.rebuild import implied_constraint_names
from sustained.schema import Check, ForeignKey, bare_table_name

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.model import Model


def _fk_action(action: Optional[str], compiler: Optional["Compiler"] = None) -> str:
    """
    An action name compared with the engine's implied NO ACTION, folded
    the way the engine folds it.
    """
    name = "NO ACTION" if action is None else action.upper()
    return name if compiler is None else compiler.equivalent_fk_action(name)


def _fk_target_matches(declared_target: str, actual: IntrospectedForeignKey) -> bool:
    """
    Whether a declared key's target names the table the catalog reports.
    The catalog reports the bare table name, plus the schema when it is
    not the connection's own. A declared target such as 'app.parents'
    names a schema, and a bare one means the connection's schema. The
    two differ when both name a schema and the names differ, or when
    only the catalog names one. A declared schema that the catalog
    leaves out is the connection's own, so it matches.
    """
    schema, _, table = declared_target.rpartition(".")
    if table.lower() != actual.target_table:
        return False
    if actual.target_schema is None:
        return True
    declared_schema = schema.rpartition(".")[2]
    return declared_schema.lower() == actual.target_schema.lower()


def _bare_reference(reference: str) -> str:
    """A column reference such as 'app.parents.id' as 'parents.id'."""
    return ".".join(reference.lower().rsplit(".", 2)[-2:])


def _fk_matches(
    declared: ForeignKey,
    actual: IntrospectedForeignKey,
    compiler: Optional["Compiler"] = None,
) -> bool:
    """
    Whether a declared foreign key and the database's row agree. The
    target and the actions only count when the engine's catalog reports
    them: a '?' target says the read cannot tell, not that they differ.
    The compiler, when given, folds actions its engine treats as one.
    """
    if tuple(c.lower() for c in declared.columns) != actual.columns:
        return False
    if actual.target_table == "?":
        return True
    if not _fk_target_matches(declared.target_table, actual):
        return False
    if actual.target_columns and (
        tuple(c.lower() for c in declared.target_columns) != actual.target_columns
    ):
        return False
    return _fk_action(declared.on_delete, compiler) == _fk_action(
        actual.on_delete, compiler
    ) and _fk_action(declared.on_update, compiler) == _fk_action(
        actual.on_update, compiler
    )


_Declared = TypeVar("_Declared")


_Actual = TypeVar("_Actual")


# One way of pairing a declared constraint with a catalog one, given the
# catalog's name for it and what the catalog read.
_PairTest = Callable[[_Declared, str, _Actual], bool]


def _pair_constraints(
    declared: Sequence[_Declared],
    actual: Mapping[str, _Actual],
    tests: Sequence[_PairTest[_Declared, _Actual]],
) -> Tuple[List[Tuple[_Declared, _Actual]], List[_Declared], List[Tuple[str, _Actual]]]:
    """
    Pairs declared constraints with the catalog's, one to one. Each test
    runs over every constraint still unpaired before the next test runs,
    so an exact match is never taken by a looser one. Returns the pairs,
    the declared constraints left without one, and the catalog's.
    """
    remaining = dict(actual)
    unpaired = list(declared)
    pairs: List[Tuple[_Declared, _Actual]] = []
    for test in tests:
        left: List[_Declared] = []
        for item in unpaired:
            name = next((n for n, a in remaining.items() if test(item, n, a)), None)
            if name is None:
                left.append(item)
            else:
                pairs.append((item, remaining.pop(name)))
        unpaired = left
    return pairs, unpaired, list(remaining.items())


def _named(constraint: Union[ForeignKey, Check], name: str, _: object) -> bool:
    return constraint.name.lower() == name


def _same_fk(fk: ForeignKey, _: str, actual: IntrospectedForeignKey) -> bool:
    return _fk_matches(fk, actual)


def _same_fk_columns(fk: ForeignKey, _: str, actual: IntrospectedForeignKey) -> bool:
    return tuple(c.lower() for c in fk.columns) == actual.columns


def _same_check(check: Check, _: str, expression: str) -> bool:
    return normalize_check(check.expression) == normalize_check(expression)


def _diff_declared_constraints(
    compiler: "Compiler",
    diff: SchemaDiff,
    model: Type["Model"],
    actual_table: IntrospectedTable,
    snapshot: Snapshot,
) -> None:
    """
    Compares the model's tableConstraints against the database's named
    constraints, on engines whose catalog reports them. A degraded read
    diffs nothing: an empty mapping is not proof of absence.

    A catalog that keeps constraint names pairs by name. DuckDB names
    every constraint itself, so there a foreign key pairs by its target
    or else its columns, and a check by its normalized expression. On
    DuckDB a difference stays a note, because no statement can change a
    constraint on a table that exists.
    """
    table_name = model.tableName or ""
    declared = model.tableConstraints or []
    declared_fks = [c for c in declared if isinstance(c, ForeignKey)]
    declared_checks = [c for c in declared if isinstance(c, Check)]
    implied_checks, implied_fk_columns = implied_constraint_names(compiler, model)
    by_name = compiler.keeps_constraint_names()
    fixed = _constraints_fixed_at_create(compiler)
    recreate = (
        "The engine cannot change a constraint on a table that exists, so "
        "recreate the table by hand."
    )

    if snapshot.constraints_read:
        fk_tests: List[_PairTest[ForeignKey, IntrospectedForeignKey]] = (
            [_named] if by_name else [_same_fk, _same_fk_columns]
        )
        fk_pairs, missing_fks, extra_fks = _pair_constraints(
            declared_fks, actual_table.foreign_keys, fk_tests
        )
        for fk in missing_fks:
            if fixed:
                diff.constraint_notes.append(
                    f"{table_name} declares foreign key '{fk.name}' that the "
                    f"database does not have. {recreate}"
                )
            else:
                diff.new_foreign_keys.append((model, fk))
        for fk, actual_fk in fk_pairs:
            if _fk_matches(fk, actual_fk, compiler):
                continue
            if fixed:
                diff.constraint_notes.append(
                    f"{table_name} foreign key '{fk.name}' points at "
                    f"{actual_fk.target_table}, the model declares "
                    f"{fk.target_table.lower()}. {recreate}"
                )
            else:
                diff.changed_foreign_keys.append((model, fk, actual_fk))
        for name, actual_fk in extra_fks:
            if actual_fk.columns in implied_fk_columns:
                continue
            if fixed:
                diff.constraint_notes.append(
                    f"{table_name} has foreign key '{name}' on "
                    f"({', '.join(actual_fk.columns)}) that no model "
                    f"declares. {recreate}"
                )
            else:
                diff.extra_foreign_keys.append(
                    (table_name, actual_fk.name or name, actual_fk)
                )

    if snapshot.checks_read:
        check_tests: List[_PairTest[Check, str]] = (
            [_named] if by_name else [_same_check]
        )
        check_pairs, missing_checks, extra_checks = _pair_constraints(
            declared_checks, actual_table.checks, check_tests
        )
        for check in missing_checks:
            if fixed:
                diff.constraint_notes.append(
                    f"{table_name} declares check '{check.name}' that the "
                    f"database does not have. {recreate}"
                )
            else:
                diff.new_checks.append((model, check))
        for check, actual_expression in check_pairs:
            if normalize_check(check.expression) == normalize_check(actual_expression):
                continue
            if compiler.supports_alter_column():
                # The engine rewrites expressions on the way in, so a
                # mismatch here is a doubt, and a doubt never drops.
                diff.constraint_notes.append(
                    f"{table_name} check '{check.name}' reads as "
                    f"{actual_expression!r}, the model declares "
                    f"{check.expression!r}"
                )
            else:
                diff.changed_checks.append((model, check, actual_expression))
        for name, expression in extra_checks:
            if name in implied_checks:
                continue
            if fixed:
                diff.constraint_notes.append(
                    f"{table_name} has check '{name}' that no model "
                    f"declares: {expression!r}. {recreate}"
                )
                continue
            # An undeclared check is a note, not a drop. Engines rewrite
            # a check expression on the way in, so a check the models do
            # write can still read as one they do not, and generation
            # must not refuse a whole diff over that doubt. allow_drops
            # still drops it.
            spelled = actual_table.check_names.get(name, name)
            diff.extra_checks.append((table_name, spelled, expression))
            diff.constraint_notes.append(
                f"{table_name} has check '{name}' that no model declares: "
                f"{expression!r}. Pass allow_drops=True to drop it."
            )


def _enum_check_values(expression: str) -> Tuple[str, ...]:
    """
    The string literals of an enum column's CHECK expression, in order.
    Sustained writes the check as column IN ('a', 'b'). SQL Server reads
    it back as ([column]=N'a' OR [column]=N'b'), and the literals are
    the values either way.
    """
    return tuple(
        value.replace("''", "'")
        for value in re.findall(r"'((?:[^']|'')*)'", expression)
    )


def _diff_enum_checks(
    diff: SchemaDiff, model: Type["Model"], actual_table: IntrospectedTable
) -> None:
    """
    Compares the values each enum column's CHECK permits with the values
    the model declares, on a dialect where an enum is a checked VARCHAR.
    A value added to the model only widens the VARCHAR when it is the
    longest one, so without this an added value would read as no change
    and every insert of it would fail.
    """
    table = bare_table_name(model.tableName or "")
    for name, coldef in (model.tableColumns or {}).items():
        if coldef.type_name != "ENUM" or name.lower() not in actual_table.columns:
            continue
        assert coldef.enum_values is not None
        expression = actual_table.checks.get(f"ck_{table}_{name}_enum".lower())
        live = () if expression is None else _enum_check_values(expression)
        if expression is None or set(live) != set(coldef.enum_values):
            diff.changed_enum_checks.append((model, name, live, expression))


def _diff_constraints(
    compiler: "Compiler",
    diff: SchemaDiff,
    model: Type["Model"],
    actual_table: IntrospectedTable,
    snapshot: Snapshot,
) -> None:
    assert model.tableColumns is not None
    table_name = model.tableName or ""

    if compiler.supports_constraints():
        _diff_declared_constraints(compiler, diff, model, actual_table, snapshot)
        if compiler.enum_strategy() == "check" and snapshot.checks_read:
            _diff_enum_checks(diff, model, actual_table)

    expected_pk = tuple(
        sorted(n.lower() for n, c in model.tableColumns.items() if c.primary_key)
    )
    actual_pk = tuple(sorted(actual_table.primary_key))
    if actual_pk and expected_pk != actual_pk:
        diff.constraint_notes.append(
            f"{table_name} primary key is ({', '.join(actual_pk)}), "
            f"model declares ({', '.join(expected_pk)})"
        )

    for name, coldef in model.tableColumns.items():
        actual_col = actual_table.columns.get(name.lower())
        if actual_col is None:
            continue
        if coldef.references is not None:
            actual_fk = actual_table.foreign_key_targets.get(name.lower())
            if actual_fk is None:
                diff.constraint_notes.append(
                    f"{table_name}.{name} declares a foreign key to "
                    f"{coldef.references} that the database does not have"
                )
            elif actual_fk not in ("?", _bare_reference(coldef.references)):
                diff.constraint_notes.append(
                    f"{table_name}.{name} foreign key targets {actual_fk}, "
                    f"model declares {coldef.references.lower()}"
                )
        if coldef.unique and not coldef.primary_key:
            covered = any(
                index.unique and index.columns == (name.lower(),)
                for index in actual_table.indexes.values()
            )
            if not covered:
                diff.constraint_notes.append(
                    f"{table_name}.{name} declares UNIQUE but the database "
                    "has no unique index on it"
                )
        if is_sequence_default(actual_col.default):
            # A serial column's default names a sequence, which no model
            # declaration can equal. There is nothing to compare.
            continue
        expected_default = (
            None if coldef.default is None else normalize_default(str(coldef.default))
        )
        actual_default = normalize_default(actual_col.default)
        if expected_default != actual_default:
            diff.constraint_notes.append(
                f"{table_name}.{name} default is "
                f"{actual_default or 'none'}, model declares "
                f"{expected_default or 'none'}"
            )
