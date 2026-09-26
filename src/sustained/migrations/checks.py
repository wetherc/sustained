"""
The checks around a run: the guards over the statements it would apply,
and the comparison of the registered migrations against the tracking
table rows.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, List, Optional, Sequence, Set, Tuple

from sustained.dialects import Dialects
from sustained.migrations.migration import (
    AppliedRecord,
    Migration,
    _checksum_matches,
    _legacy_checksum,
    migration_checksum,
    migration_sql,
)

if TYPE_CHECKING:
    from sustained.analysis import MigrationStatement
    from sustained.compilers.base import Compiler
    from sustained.guards import Guard, Verdict


def run_statements(
    run: Sequence[Migration], compiler: Optional["Compiler"] = None
) -> List["MigrationStatement"]:
    """
    Every up statement a run would apply, in order. Callable steps render
    no SQL and are skipped, so a guard cannot read them, the same limit
    the destructive labels carry. Ddl steps render for the given
    compiler's dialect, which is why the guards can read them.

    Each statement carries the id of the migration it came from and that
    migration's transaction flag, so a guard can see where one migration
    ends and the next begins. The values are strings, so a guard that
    reads them as such needs to know nothing about this.
    """
    from sustained.analysis import MigrationStatement

    statements: List["MigrationStatement"] = []
    for migration in run:
        if callable(migration.up):
            continue
        statements.extend(
            MigrationStatement(sql, migration.id, migration.transactional)
            for sql in migration_sql(migration, "up", compiler)
        )
    return statements


def _report_warnings(verdicts: Sequence["Verdict"]) -> None:
    """Prints the warning verdicts on stderr, one per line."""
    for verdict in verdicts:
        print(f"warn: {verdict.rule}  {verdict.statement}", file=sys.stderr)


def check_guards(
    guards: Sequence["Guard"],
    run: Sequence[Migration],
    dialect: Dialects,
    reported: Optional[Set["Verdict"]] = None,
) -> None:
    """
    Runs the guards over the statements a run would apply. A blocking
    verdict raises GuardBlocked before anything executes; warnings print
    on stderr and the run goes on.

    `reported` collects the warnings already printed. A run whose
    statements are known in two parts checks the whole set twice, and the
    set keeps the operator from reading the same warning twice.
    """
    from sustained.exceptions import GuardBlocked
    from sustained.guards import blocking, run_guards, warnings_only

    if not guards:
        return
    compiler = Dialects.get_compiler(dialect)
    verdicts = run_guards(guards, run_statements(run, compiler), dialect)
    blockers = blocking(verdicts)
    if blockers:
        raise GuardBlocked(blockers)
    warned = warnings_only(verdicts)
    if reported is not None:
        warned = [v for v in warned if v not in reported]
        reported.update(warned)
    _report_warnings(warned)


def _failed_attempt_problem(migration_id: str) -> str:
    """The validation problem for a row a failed up or down step left."""
    return (
        f"migration '{migration_id}' has a failed attempt on record; "
        "clean up any partial changes, then run repair() and retry"
    )


def _checksum_repair(
    record: AppliedRecord, migration: Migration
) -> Optional[Tuple[str, str]]:
    """
    The checksum repair() writes on a row and the action it reports, or
    None when the row needs no rewrite. A row that stores the legacy
    checksum of the same statements is rewritten in the current format,
    so a later split of the migration reads as an edit.

    A repeatable's row is rewritten only in that case. Its statements
    match, so no re-run is pending. A repeatable whose statements changed
    keeps its stored checksum, because rewriting it would cancel the
    re-run the change scheduled.
    """
    current = migration_checksum(migration)
    if current is None or current == record.checksum:
        return None
    if record.checksum is not None and record.checksum == _legacy_checksum(migration):
        return current, f"updated the checksum format of '{record.id}'"
    if migration.repeatable:
        return None
    return current, f"updated the stored checksum of '{record.id}'"


def _is_current(
    record: Optional[AppliedRecord], migration: Migration, repeatable: bool
) -> bool:
    """True when the tracking row makes a run unnecessary."""
    if record is None or not record.success:
        return False
    if not repeatable:
        return True
    return _checksum_matches(record.checksum, migration)


def _migration_state(record: Optional[AppliedRecord], migration: Migration) -> str:
    """One migration's state: 'applied', 'pending', or 'changed'."""
    if record is None or not record.success:
        return "pending"
    if migration.repeatable and not _checksum_matches(record.checksum, migration):
        return "changed"
    return "applied"


def _validation_problems(
    migrations: List[Migration],
    records: List[AppliedRecord],
    allow_out_of_order: bool = False,
    require_registered: bool = True,
) -> List[str]:
    """
    Compares the registered migrations against the tracking table rows and
    describes every inconsistency: failed attempts, applied migrations the
    migrator does not know, edited migrations whose checksum no longer
    matches, and pending migrations ordered before applied ones. Passing
    require_registered=False skips the unknown-id check.

    A row marked generated is never reported as unknown. It was written
    by a diff against the models, so no file or list carries its id, and
    a later run regenerates whatever difference is left.
    """
    problems: List[str] = []
    registered = {m.id: m for m in migrations}
    applied_ids = {r.id for r in records if r.success}

    for record in records:
        if not record.success:
            problems.append(_failed_attempt_problem(record.id))
    for record in records:
        if not record.success:
            continue
        migration = registered.get(record.id)
        if migration is None:
            if require_registered and not record.generated:
                problems.append(
                    f"applied migration '{record.id}' is not registered "
                    "with this migrator"
                )
            continue
        if migration.repeatable:
            # A changed checksum is the re-run signal, not a problem.
            continue
        if (
            migration_checksum(migration) is not None
            and record.checksum is not None
            and not _checksum_matches(record.checksum, migration)
        ):
            problems.append(
                f"checksum mismatch for '{record.id}': the migration "
                "changed after it was applied; restore it, or run repair() "
                "to accept the new contents"
            )
    if not allow_out_of_order:
        first_pending: Optional[str] = None
        for migration in migrations:
            if migration.repeatable:
                continue
            if migration.id not in applied_ids:
                if first_pending is None:
                    first_pending = migration.id
            elif first_pending is not None:
                problems.append(
                    f"pending migration '{first_pending}' is ordered before "
                    f"applied migration '{migration.id}'; pass "
                    "allow_out_of_order=True to apply it anyway"
                )
                break
    return problems


def _changed_since_applied(
    migration: Migration, record: Optional[AppliedRecord]
) -> bool:
    """
    True when the migration's statements no longer match the checksum the
    tracking row holds. A row without a checksum, and a callable step with
    no checksum to compute, compare as unchanged.
    """
    if record is None or record.checksum is None:
        return False
    return migration_checksum(migration) is not None and not _checksum_matches(
        record.checksum, migration
    )


def _changed_down_message(migration_id: str) -> str:
    """The error for a revert of a migration that was edited."""
    return (
        f"Migration '{migration_id}' changed after it was applied, so its "
        "down step may not reverse the statements that ran. Restore the "
        "migration, run repair() to accept the new contents, or pass "
        "allow_changed=True to revert it as it stands now."
    )
