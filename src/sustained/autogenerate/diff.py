"""
The schema diff: SchemaDiff, and the comparisons of tables, columns,
indexes, and enum types that fill it. Constraints diff in
sustained.autogenerate.constraints.
"""

from __future__ import annotations

import re
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
)

from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
    normalize_type,
    parse_inline_enum,
    type_params,
)
from sustained.schema import Check, ForeignKey, bare_table_name, collect_enum_types

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.model import Model
    from sustained.schema import ColumnDef, Index


class SchemaDiff:
    """The differences between declared models and the live database."""

    def __init__(self) -> None:
        self.missing_tables: List[Type["Model"]] = []
        self.new_columns: List[Tuple[Type["Model"], str, "ColumnDef"]] = []
        self.extra_tables: List[str] = []
        self.extra_columns: List[Tuple[str, str]] = []
        self.changed_columns: List[Tuple[str, str, str, str]] = []
        self.changed_comments: List[Tuple[str, str, Optional[str], Optional[str]]] = []
        self.new_indexes: List[Tuple[Type["Model"], "Index"]] = []
        self.extra_indexes: List[Tuple[str, str, IntrospectedIndex]] = []
        self.changed_indexes: List[Tuple[Type["Model"], "Index", IntrospectedIndex]] = (
            []
        )
        self.new_enum_types: List[Tuple[str, Tuple[str, ...]]] = []
        self.changed_enum_types: List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = []
        self.new_foreign_keys: List[Tuple[Type["Model"], ForeignKey]] = []
        self.changed_foreign_keys: List[
            Tuple[Type["Model"], ForeignKey, IntrospectedForeignKey]
        ] = []
        self.extra_foreign_keys: List[Tuple[str, str, IntrospectedForeignKey]] = []
        self.new_checks: List[Tuple[Type["Model"], Check]] = []
        self.changed_checks: List[Tuple[Type["Model"], Check, str]] = []
        # An enum column on a check-strategy dialect whose CHECK permits
        # other values than the model declares: the model, the column,
        # the values the check permits, and its expression, or None
        # when the check is missing.
        self.changed_enum_checks: List[
            Tuple[Type["Model"], str, Tuple[str, ...], Optional[str]]
        ] = []
        self.extra_checks: List[Tuple[str, str, str]] = []
        # Named enum types that only extra tables and columns use and no
        # model declares, spelled as the catalog spells them. They drop
        # with those tables and columns.
        self.extra_enum_types: List[str] = []
        self.constraint_notes: List[str] = []

    def is_empty(self) -> bool:
        return not (
            self.missing_tables
            or self.new_columns
            or self.extra_tables
            or self.extra_columns
            or self.changed_columns
            or self.changed_comments
            or self.new_indexes
            or self.extra_indexes
            or self.changed_indexes
            or self.new_enum_types
            or self.changed_enum_types
            or self.new_foreign_keys
            or self.changed_foreign_keys
            or self.extra_foreign_keys
            or self.new_checks
            or self.changed_checks
            or self.changed_enum_checks
            or self.extra_checks
            or self.constraint_notes
        )

    def outstanding(self, ignore_changed_columns: bool = False) -> List[str]:
        """
        The differences a generated migration was supposed to close, one
        readable line each, empty when the models all landed.

        Only objects the models declare are reported. A table, column, or
        index the database holds and the models do not is left out: a
        generated migration leaves those alone unless drops are allowed,
        so counting them would report a schema built partly by hand as a
        failure.

        Pass ignore_changed_columns=True to leave type and nullability
        changes out, matching a migration generated with the same option:
        those columns were never meant to be closed.
        """
        lines: List[str] = []
        for model in self.missing_tables:
            lines.append(f"table '{model.tableName}' was not created")
        for model, name, _ in self.new_columns:
            lines.append(f"column '{model.tableName}.{name}' was not added")
        if not ignore_changed_columns:
            for table, name, actual, expected in self.changed_columns:
                lines.append(
                    f"column '{table}.{name}' is {actual}, "
                    f"the models declare {expected}"
                )
        for table, name, actual_comment, expected_comment in self.changed_comments:
            actual_text = "none" if actual_comment is None else repr(actual_comment)
            expected_text = (
                "none" if expected_comment is None else repr(expected_comment)
            )
            lines.append(
                f"column '{table}.{name}' comment is {actual_text}, "
                f"the models declare {expected_text}"
            )
        for model, index in self.new_indexes:
            lines.append(f"index '{index.name}' on '{model.tableName}' was not created")
        for model, index, _ in self.changed_indexes:
            lines.append(f"index '{index.name}' on '{model.tableName}' was not rebuilt")
        for type_name, _ in self.new_enum_types:
            lines.append(f"enum type '{type_name}' was not created")
        for type_name, live_values, declared_values in self.changed_enum_types:
            lines.append(
                f"enum type '{type_name}' has values "
                f"({', '.join(live_values)}), the models declare "
                f"({', '.join(declared_values)})"
            )
        # Changed and extra constraints stay out: their migration steps
        # are gated by allow_drops, so a run that left them alone may
        # still have landed everything it promised.
        for model, fk in self.new_foreign_keys:
            lines.append(
                f"foreign key '{fk.name}' on '{model.tableName}' was not added"
            )
        for model, check in self.new_checks:
            lines.append(f"check '{check.name}' on '{model.tableName}' was not added")
        for model, name, live_values, _ in self.changed_enum_checks:
            lines.append(
                f"enum column '{model.tableName}.{name}' permits "
                f"({', '.join(live_values)}), the models declare "
                f"({', '.join(_declared_enum_values(model, name))})"
            )
        return lines

    def summary(self) -> str:
        """A human-readable description of every difference."""
        lines: List[str] = []
        for type_name, _ in self.new_enum_types:
            lines.append(f"create enum type {type_name}")
        for type_name, live_values, declared_values in self.changed_enum_types:
            additions = _enum_value_additions(live_values, declared_values)
            if additions is not None:
                for value in additions:
                    lines.append(f"add value '{value}' to enum type {type_name}")
            else:
                lines.append(
                    f"change enum type {type_name}: database has "
                    f"({', '.join(live_values)}), model declares "
                    f"({', '.join(declared_values)})"
                )
        for model in self.missing_tables:
            lines.append(f"create table {model.tableName}")
        for model, name, _ in self.new_columns:
            lines.append(f"add column {model.tableName}.{name}")
        for model, index in self.new_indexes:
            lines.append(f"create index {index.name} on {model.tableName}")
        for model, index, _ in self.changed_indexes:
            lines.append(f"rebuild index {index.name} on {model.tableName}")
        for table in self.extra_tables:
            lines.append(f"drop table {table} (destructive)")
        for table, name in self.extra_columns:
            lines.append(f"drop column {table}.{name} (destructive)")
        for table, name, actual_index in self.extra_indexes:
            kind = "unique constraint" if actual_index.constraint else "index"
            lines.append(f"drop {kind} {name} on {table}")
        for model, fk in self.new_foreign_keys:
            lines.append(f"add foreign key {fk.name} on {model.tableName}")
        for model, check in self.new_checks:
            lines.append(f"add check {check.name} on {model.tableName}")
        for model, fk, _ in self.changed_foreign_keys:
            lines.append(
                f"change foreign key {fk.name} on {model.tableName} (destructive)"
            )
        for model, check, _ in self.changed_checks:
            lines.append(f"change check {check.name} on {model.tableName}")
        for model, name, live_values, _ in self.changed_enum_checks:
            lines.append(
                f"change the values of enum column {model.tableName}.{name}: "
                f"database permits ({', '.join(live_values)}), model declares "
                f"({', '.join(_declared_enum_values(model, name))})"
            )
        for table, name, _ in self.extra_foreign_keys:
            lines.append(f"drop foreign key {name} on {table} (destructive)")
        for type_name in self.extra_enum_types:
            lines.append(f"drop enum type {type_name} (destructive)")
        for table, name, actual, expected in self.changed_columns:
            lines.append(
                f"change column {table}.{name}: database has {actual}, "
                f"model declares {expected}"
            )
        for table, name, actual_comment, expected_comment in self.changed_comments:
            if expected_comment is None:
                lines.append(f"clear the comment on {table}.{name}")
            else:
                lines.append(f"set the comment on {table}.{name}")
        for note in self.constraint_notes:
            lines.append(f"note: {note} (not auto-migrated)")
        return "\n".join(lines) if lines else "schema up to date"


def _declared_enum_values(model: Type["Model"], name: str) -> Tuple[str, ...]:
    """The values a model's enum column declares."""
    values = (model.tableColumns or {})[name].enum_values
    assert values is not None
    return values


def _enum_value_additions(
    actual: Tuple[str, ...], expected: Tuple[str, ...]
) -> Optional[Tuple[str, ...]]:
    """
    The values the models append to an enum type's existing list, or None
    when the change is not a pure append. Postgres adds a value in place
    but never removes or reorders one, so only an appended tail can be
    generated.
    """
    if len(expected) > len(actual) and expected[: len(actual)] == actual:
        return expected[len(actual) :]
    return None


def _declared_enum_types(
    models: List[Type["Model"]],
) -> Dict[str, Tuple[str, ...]]:
    """
    Every enum type the models declare, name to value tuple, merged
    across models. The same name declared with different values in two
    models raises: it would be one database object with two definitions.
    """
    types: Dict[str, Tuple[str, ...]] = {}
    for model in models:
        for type_name, values in collect_enum_types(model.tableColumns or {}).items():
            known = types.get(type_name)
            if known is not None and known != values:
                raise ValueError(
                    f"Enum '{type_name}' is declared with different "
                    f"values in two models: {', '.join(known)} versus "
                    f"{', '.join(values)}."
                )
            types[type_name] = values
    return types


def _diff_enum_types(
    diff: SchemaDiff,
    declared: Dict[str, Type["Model"]],
    declared_types: Dict[str, Tuple[str, ...]],
    actual: Snapshot,
) -> None:
    """
    Compares the models' enum types against the database, on dialects
    where an enum is a named type object. With a catalog read, absence
    and value changes come straight from it: Postgres reads pg_enum and
    DuckDB reads duckdb_types(). Without one, a type is taken as present
    when a column of it already exists, and its live values are read
    from the column's own inline type spelling when the engine writes
    one. That fallback cannot see a type no column uses.
    """
    if actual.enum_types_read:
        for type_name, values in declared_types.items():
            existing = actual.enum_types.get(type_name.lower())
            if existing is None:
                diff.new_enum_types.append((type_name, values))
            elif existing != values:
                diff.changed_enum_types.append((type_name, existing, values))
        return
    present: Set[str] = set()
    changed: Set[str] = set()
    for table_key, model in declared.items():
        actual_table = actual.get(table_key)
        if actual_table is None:
            continue
        for name, coldef in (model.tableColumns or {}).items():
            if coldef.type_name != "ENUM":
                continue
            actual_col = actual_table.columns.get(name.lower())
            if actual_col is None or not _actual_column_is_enum(actual_col, coldef):
                continue
            assert coldef.enum_name is not None and coldef.enum_values is not None
            present.add(coldef.enum_name.lower())
            live_values = actual_col.enum_values or parse_inline_enum(
                actual_col.raw_type
            )
            if (
                live_values
                and live_values != coldef.enum_values
                and coldef.enum_name.lower() not in changed
            ):
                changed.add(coldef.enum_name.lower())
                diff.changed_enum_types.append(
                    (coldef.enum_name, tuple(live_values), coldef.enum_values)
                )
    for type_name, values in declared_types.items():
        if type_name.lower() not in present:
            diff.new_enum_types.append((type_name, values))


def _actual_column_is_enum(actual_col: IntrospectedColumn, coldef: "ColumnDef") -> bool:
    """
    Whether the live column already holds the declared enum type. The
    catalog says so directly when it names enum types; otherwise the
    column's raw type either is the type's own name or spells the value
    list inline, as DuckDB's information_schema does.
    """
    assert coldef.enum_name is not None
    if actual_col.enum_name is not None:
        return actual_col.enum_name == coldef.enum_name.lower()
    if actual_col.raw_type.lower().strip('"`[]') == coldef.enum_name.lower():
        return True
    return bool(parse_inline_enum(actual_col.raw_type))


def _rename_in_expression(expression: str, old: str, new: str) -> str:
    """
    The expression with every reference to the column `old` rewritten to
    `new`. Bare and quoted identifiers are rewritten; text inside
    single-quoted string literals is left alone.
    """
    identifier = re.compile(rf"([\"`\[]?)\b{re.escape(old)}\b([\"`\]]?)", re.IGNORECASE)
    parts = re.split(r"('(?:[^']|'')*')", expression)
    for index in range(0, len(parts), 2):
        parts[index] = identifier.sub(rf"\g<1>{new}\g<2>", parts[index])
    return "".join(parts)


def _apply_renames(
    actual: Dict[str, IntrospectedTable],
    renames: Dict[str, str],
    table_renames: Dict[str, str],
) -> None:
    """
    Rewrites the introspected schema as if the renames had already run, so
    renamed objects do not diff as drop-plus-add.
    """
    for old, new in table_renames.items():
        old_key, new_key = old.lower(), new.lower()
        if old_key not in actual:
            raise ValueError(f"Cannot rename unknown table '{old}'.")
        actual[new_key] = actual.pop(old_key)._replace(name=new)
        # The engine points a child's foreign keys at the renamed table.
        # A SQLite rebuild writes the stored triggers back, and the
        # rename that runs first has rewritten them in the database.
        for key, other in actual.items():
            actual[key] = other._replace(
                foreign_keys={
                    name: (
                        fk._replace(target_table=new_key)
                        if fk.target_table == old_key
                        else fk
                    )
                    for name, fk in other.foreign_keys.items()
                },
                triggers=tuple(
                    _rename_in_expression(sql, old_key, new_key)
                    for sql in other.triggers
                ),
            )
    for path, new_name in renames.items():
        if "." not in path:
            raise ValueError(
                f"Column rename keys must be 'table.column', got {path!r}."
            )
        table, old_name = path.rsplit(".", 1)
        table_key = table.lower()
        old_key, new_key = old_name.lower(), new_name.lower()
        if table_key not in actual or old_key not in actual[table_key].columns:
            raise ValueError(f"Cannot rename unknown column '{path}'.")
        old_table = actual[table_key]
        columns = old_table.columns
        columns[new_key] = columns.pop(old_key)._replace(name=new_name)
        # Engines rewrite the column name inside indexes, keys, and
        # constraints on rename; mirror that so nothing diffs as changed.
        renamed_indexes = {
            name: index._replace(
                columns=tuple(new_key if c == old_key else c for c in index.columns)
            )
            for name, index in old_table.indexes.items()
        }
        renamed_fks = {
            name: fk._replace(
                columns=tuple(new_key if c == old_key else c for c in fk.columns)
            )
            for name, fk in old_table.foreign_keys.items()
        }
        renamed_checks = {
            name: _rename_in_expression(expression, old_key, new_key)
            for name, expression in old_table.checks.items()
        }
        renamed_unnamed = tuple(
            _rename_in_expression(expression, old_key, new_key)
            for expression in old_table.unnamed_checks
        )
        renamed_triggers = tuple(
            _rename_in_expression(sql, old_key, new_key) for sql in old_table.triggers
        )
        actual[table_key] = old_table._replace(
            primary_key=tuple(
                new_key if c == old_key else c for c in old_table.primary_key
            ),
            foreign_keys=renamed_fks,
            indexes=renamed_indexes,
            checks=renamed_checks,
            unnamed_checks=renamed_unnamed,
            triggers=renamed_triggers,
        )
        # A key that points at the renamed column follows it the same way.
        for key, other in actual.items():
            actual[key] = other._replace(
                foreign_keys={
                    name: (
                        fk._replace(
                            target_columns=tuple(
                                new_key if c == old_key else c
                                for c in fk.target_columns
                            )
                        )
                        if fk.target_table == table_key
                        else fk
                    )
                    for name, fk in other.foreign_keys.items()
                }
            )


def _orphaned_enum_types(
    actual: Snapshot,
    extra_tables: List[str],
    extra_columns: List[Tuple[str, str]],
    declared_types: Dict[str, Tuple[str, ...]],
) -> List[str]:
    """
    The named enum types that a dropped table or column uses and nothing
    else does. A type any model declares stays, and so does a type a
    remaining column may use.

    Postgres names a column's type in the catalog. DuckDB reports only
    the value list, so a column is matched to a type by its values. A
    dropped column whose values match two types names neither, and a
    remaining column whose values match a type keeps it.
    """
    dropped_tables = {table.lower() for table in extra_tables}
    dropped_columns = {(table.lower(), name.lower()) for table, name in extra_columns}
    by_values: Dict[Tuple[str, ...], List[str]] = {}
    for key, values in actual.enum_types.items():
        by_values.setdefault(values, []).append(key)

    def named_type(column: IntrospectedColumn) -> Optional[str]:
        key = column.enum_name or column.raw_type.lower()
        return key if key in actual.enum_types else None

    candidates: Dict[str, str] = {}
    remaining: Set[str] = set()
    for table_key, table in actual.items():
        for column_key, column in table.columns.items():
            named = named_type(column)
            matches = (
                [named]
                if named
                else by_values.get(parse_inline_enum(column.raw_type), [])
            )
            if (
                table_key in dropped_tables
                or (table_key, column_key) in dropped_columns
            ):
                if len(matches) == 1:
                    # Postgres keeps the spelling in the column's type.
                    candidates.setdefault(
                        matches[0], column.raw_type if named else matches[0]
                    )
            else:
                remaining.update(matches)
    declared = {name.lower() for name in declared_types}
    return [
        spelled
        for key, spelled in candidates.items()
        if key not in remaining and key not in declared
    ]


def _comment_or_none(comment: Optional[str]) -> Optional[str]:
    """An empty comment read as None: MySQL spells no comment as ''."""
    return None if comment is None or comment == "" else comment


def _diff_columns(
    compiler: "Compiler",
    diff: SchemaDiff,
    model: Type["Model"],
    actual_table: IntrospectedTable,
    snapshot: Snapshot,
) -> None:
    assert model.tableColumns is not None
    table_name = model.tableName or ""
    for name, coldef in model.tableColumns.items():
        actual_col = actual_table.columns.get(name.lower())
        if actual_col is None:
            diff.new_columns.append((model, name, coldef))
            continue
        # Comments diff only when the catalog read reported them; an
        # absent comment on a degraded read is not proof of absence.
        if snapshot.comments_read:
            actual_comment = _comment_or_none(actual_col.comment)
            expected_comment = _comment_or_none(coldef.comment)
            if actual_comment != expected_comment:
                diff.changed_comments.append(
                    (table_name, name, actual_comment, expected_comment)
                )
        expected_rendered = compiler.compile_column_type(coldef)
        type_changed = _column_type_changed(
            compiler, coldef, expected_rendered, actual_col
        )
        # SQLite reports INTEGER PRIMARY KEY as nullable, so nullability
        # is only compared on non-key columns.
        null_changed = (
            not coldef.primary_key
            and not actual_col.primary_key
            and actual_col.nullable != coldef.nullable
        )
        if type_changed or null_changed:
            expected_desc = expected_rendered.upper() + (
                "" if coldef.nullable else " NOT NULL"
            )
            actual_desc = (actual_col.raw_type or "?").upper() + (
                "" if actual_col.nullable else " NOT NULL"
            )
            diff.changed_columns.append((table_name, name, actual_desc, expected_desc))
    declared_names = {c.lower() for c in model.tableColumns}
    for name in actual_table.columns:
        if name not in declared_names:
            diff.extra_columns.append((table_name, actual_table.spelled_column(name)))


def _column_type_changed(
    compiler: "Compiler",
    coldef: "ColumnDef",
    expected_rendered: str,
    actual_col: IntrospectedColumn,
) -> bool:
    """
    Whether a live column's type differs from the type the model
    declares. The logical types are compared first. Length and precision
    parameters only count when both sides carry them: an engine that
    reports DATETIME for a column created as DATETIME(6) says nothing
    about the precision, and treating that silence as a change would
    rewrite the column and drop its default.

    The diff and the step generator both call this, so a column can
    never diff as changed and then generate a statement for a different
    change.
    """
    if coldef.type_name == "ENUM" and compiler.enum_strategy() == "native":
        # A native enum column matches on its type's name; value
        # differences live on the type itself and are reported in
        # changed_enum_types, not here.
        return not _actual_column_is_enum(actual_col, coldef)
    if coldef.type_name == "ENUM" and compiler.enum_strategy() == "inline":
        # type_params() uppercases, which folds 'open' and 'Open' into one
        # value list, so the values are compared as MySQL reports them.
        live_values = parse_inline_enum(actual_col.raw_type)
        if live_values:
            return live_values != tuple(coldef.enum_values or ())
    if compiler.normalize_diff_type(
        normalize_type(expected_rendered)
    ) != compiler.normalize_diff_type(normalize_type(actual_col.raw_type)):
        return True
    expected_params = type_params(expected_rendered)
    actual_params = type_params(actual_col.raw_type)
    return (
        expected_params is not None
        and actual_params is not None
        and expected_params != actual_params
    )


def _diff_indexes(
    compiler: "Compiler",
    diff: SchemaDiff,
    model: Type["Model"],
    actual_table: IntrospectedTable,
) -> None:
    declared_indexes = {i.name.lower(): i for i in model.indexes or []}
    # The catalog reports column names lowercased, so the declaration is
    # keyed the same way. A model that spells a column 'Email' still
    # exempts the unique index behind it.
    declared_columns = {
        name.lower(): coldef for name, coldef in (model.tableColumns or {}).items()
    }
    for name, index in declared_indexes.items():
        actual_index = actual_table.indexes.get(name)
        if actual_index is None:
            diff.new_indexes.append((model, index))
        elif (
            tuple(c.lower() for c in index.columns) != actual_index.columns
            or index.unique != actual_index.unique
        ):
            diff.changed_indexes.append((model, index, actual_index))
    for name, actual_index in actual_table.indexes.items():
        if name in declared_indexes:
            continue
        # SQLite names the index behind a UNIQUE constraint itself. One
        # over several columns has no declaration to diff against, and
        # one on an undeclared column goes when that column goes.
        if name.startswith("sqlite_autoindex") and (
            len(actual_index.columns) != 1
            or actual_index.columns[0] not in declared_columns
        ):
            continue
        # An engine that requires an index behind a foreign key creates
        # one named after the constraint. It belongs to the key, not to
        # the model's index list, and dropping it would break the key.
        if name in actual_table.foreign_keys:
            continue
        # Unique indexes backing a declared column-level UNIQUE or the
        # primary key are not extras.
        if actual_index.unique and len(actual_index.columns) == 1:
            column = actual_index.columns[0]
            coldef = declared_columns.get(column)
            if coldef is not None and (coldef.unique or coldef.primary_key):
                continue
        if actual_index.constraint and _constraints_fixed_at_create(compiler):
            diff.constraint_notes.append(
                f"{model.tableName} has unique constraint '{name}' on "
                f"({', '.join(actual_index.columns)}) that no model declares. "
                "The engine cannot drop a constraint from a table that "
                "exists, so recreate the table by hand."
            )
            continue
        diff.extra_indexes.append(
            (model.tableName or "", actual_index.name or name, actual_index)
        )


def _constraints_fixed_at_create(compiler: "Compiler") -> bool:
    """
    Whether constraints on a table that exists cannot change at all.
    DuckDB alters columns in place but refuses both ADD CONSTRAINT and
    DROP CONSTRAINT, and it has no table rebuild to route them through.
    SQLite also refuses them, but its rebuild can carry the change.
    """
    return not compiler.supports_add_constraint() and compiler.supports_alter_column()


def _foreign_key_targets(model: Type["Model"]) -> List[str]:
    """
    Every table a model's foreign keys point at, lowercased and in
    declaration order. The references shorthand on a column and a
    declared ForeignKey both count.
    """
    targets: List[str] = []
    for coldef in (model.tableColumns or {}).values():
        if coldef.references is not None:
            table = coldef.references.rsplit(".", 1)[0]
            targets.append(bare_table_name(table).lower())
    for constraint in model.tableConstraints or []:
        if isinstance(constraint, ForeignKey):
            targets.append(bare_table_name(constraint.target_table).lower())
    return targets


def _dependency_order(
    keys: Sequence[str], targets: Callable[[str], List[str]]
) -> Tuple[List[str], List[List[str]]]:
    """
    The keys in an order that puts each one after the keys it points at,
    and every cycle found on the way, as the path around it. Keys are
    walked in the order given and each one's targets before itself, so
    the result is the same on every run. The keys in a cycle keep their
    order. `targets` names the keys one key points at, itself left out.
    """
    ordered: List[str] = []
    state: Dict[str, bool] = {}
    cycles: List[List[str]] = []
    # The walk keeps its own stack. A chain of a thousand tables, each
    # pointing at the next, is a thousand frames deep on the recursion
    # Python allows, and the diff would end in RecursionError.
    for start in keys:
        # Each entry is (key, the path that reached it, whether the keys
        # it points at are done). The second visit places it.
        stack: List[Tuple[str, List[str], bool]] = [(start, [], False)]
        while stack:
            key, path, placing = stack.pop()
            if placing:
                state[key] = True
                ordered.append(key)
                continue
            finished = state.get(key)
            if finished:
                continue
            if finished is False:
                cycle = path[path.index(key) :] + [key]
                if cycle not in cycles:
                    cycles.append(cycle)
                continue
            state[key] = False
            stack.append((key, path, True))
            for target in reversed(targets(key)):
                stack.append((target, path + [key], False))
    return ordered, cycles


def _ordered_missing_tables(
    models: List[Type["Model"]], notes: List[str]
) -> List[Type["Model"]]:
    """
    The missing tables in an order that creates a table after the tables
    it points at. A cycle cannot be ordered; it is reported in `notes`
    and the tables in it keep their declared order.
    """
    by_key: Dict[str, Type["Model"]] = {
        (model.tableName or "").lower(): model for model in models
    }
    ordered, cycles = _dependency_order(
        list(by_key),
        lambda key: [
            target
            for target in _foreign_key_targets(by_key[key])
            if target in by_key and target != key
        ],
    )
    for cycle in cycles:
        notes.append(
            "tables reference each other in a cycle and cannot be "
            f"created in dependency order: {' -> '.join(cycle)}"
        )
    return [by_key[key] for key in ordered]
