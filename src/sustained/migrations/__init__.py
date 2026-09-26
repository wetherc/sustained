"""
Explicit, ordered schema migrations.

A Migration pairs an id with an up step and an optional down step. Steps
are a SQL string, a list of SQL strings, or a callable that receives the
connection. The Migrator applies pending migrations in order, records each
applied id in a tracking table, and reverts through the down steps.

The tracking table stores one row per applied migration: the id, a
monotonic sequence number, a SHA-256 checksum of the up step, the apply
timestamp, the execution time in milliseconds, and a success flag.
Tracking tables written by earlier versions of Sustained, which held only
the id and the timestamp, are upgraded in place on first use.

A second table holds rehearsal rows: one row per set of statements a
rehearsal proved, keyed to the applied history it started from. A run that
would remove data reads that table first and stops when nothing covers it.

Migrations are written by hand, generated from a model with
create_table_migration(), or produced by schema diffing through
sustained.autogenerate and Migrator.up(models=[...]).

The package splits the work across modules: the migration model
(`migration`), rehearsals (`rehearsal`), the tracking table (`tracking`),
the checks around a run (`checks`), recorded schema reads (`replay`),
offline planning (`planning`), the runs Migrator and AsyncMigrator share,
written once as generators (`core`), and the Migrator itself (`migrator`).
Every name, the private helpers AsyncMigrator shares included, imports
from here as it did when this was one module.
"""

from __future__ import annotations

import warnings
from typing import Any

from sustained.migrations.checks import (
    _changed_down_message,
    _changed_since_applied,
    _checksum_repair,
    _failed_attempt_problem,
    _is_current,
    _migration_state,
    _report_warnings,
    _validation_problems,
    check_guards,
    run_statements,
)
from sustained.migrations.migration import (
    _DERIVE,
    AppliedRecord,
    CallbackResult,
    Callbacks,
    CallbackTarget,
    Migration,
    MigrationStep,
    _call_on_error,
    _checked_steps,
    _checksum_matches,
    _default_compiler,
    _derived_down,
    _DeriveDown,
    _legacy_checksum,
    _render_elements,
    _restore_migration,
    _run_step,
    _step_elements,
    _stored_steps,
    _tag_applied,
    _tag_migration,
    checked_unique_ids,
    create_table_migration,
    migration_checksum,
    migration_sql,
)
from sustained.migrations.migrator import Migrator
from sustained.migrations.planning import (
    drift_lines,
    generated_id,
    plan_migration,
    render_script,
)
from sustained.migrations.rehearsal import (
    _REHEARSABLE,
    NOT_REHEARSABLE,
    REHEARSAL_FAILED,
    REHEARSAL_OVERRIDE,
    REHEARSAL_PASSED,
    Digest,
    Rehearsal,
    RehearsalResult,
    _applied_digest,
    _check_rehearsable,
    _destructive_in,
    _destructive_prefix_keys,
    _digest_migration,
    _down_sweep,
    _history_digest,
    _legacy_rehearsal_key,
    _passed_rehearsal_keys,
    _rehearsal_message,
    _rehearsal_results,
    _rehearsal_token,
    _rehearsal_writes,
    _reversal_provable,
    _scratch_rehearsal_keys,
    _skipped_results,
    rehearsal_failed,
    rehearsal_key,
)
from sustained.migrations.replay import (
    SchemaRead,
    _is_read_savepoint,
    _ReadStep,
    _ReplayConnection,
    _ReplayCursor,
)
from sustained.migrations.tracking import (
    _UPGRADE_COLUMNS,
    _lock_message,
    _lock_row,
    _next_seq,
    _rehearsal_column_defs,
    _tracking_column_defs,
    _unlock_message,
    _upgrade_column_def,
    insert_sql,
    quoted_columns,
    records_from_rows,
    records_select,
    update_sql,
)

# Every name the single module used to define, the private ones included:
# aio_migrations and the tests import them from here.
__all__ = [
    # from migration
    "CallbackTarget",
    "CallbackResult",
    "MigrationStep",
    "_DeriveDown",
    "_DERIVE",
    "_step_elements",
    "_default_compiler",
    "_render_elements",
    "_derived_down",
    "Callbacks",
    "Migration",
    "migration_checksum",
    "_legacy_checksum",
    "_checksum_matches",
    "AppliedRecord",
    "checked_unique_ids",
    "_checked_steps",
    "migration_sql",
    "_run_step",
    "create_table_migration",
    "_stored_steps",
    "_restore_migration",
    "_tag_migration",
    "_tag_applied",
    "_call_on_error",
    # from rehearsal
    "RehearsalResult",
    "rehearsal_failed",
    "Rehearsal",
    "REHEARSAL_PASSED",
    "REHEARSAL_FAILED",
    "REHEARSAL_OVERRIDE",
    "_rehearsal_token",
    "Digest",
    "_history_digest",
    "_applied_digest",
    "_digest_migration",
    "rehearsal_key",
    "_legacy_rehearsal_key",
    "_destructive_in",
    "_destructive_prefix_keys",
    "_passed_rehearsal_keys",
    "_rehearsal_writes",
    "_scratch_rehearsal_keys",
    "_rehearsal_message",
    "_REHEARSABLE",
    "_check_rehearsable",
    "_down_sweep",
    "_reversal_provable",
    "_rehearsal_results",
    "NOT_REHEARSABLE",
    "_skipped_results",
    # from tracking
    "_UPGRADE_COLUMNS",
    "quoted_columns",
    "records_select",
    "insert_sql",
    "update_sql",
    "_tracking_column_defs",
    "_rehearsal_column_defs",
    "_upgrade_column_def",
    "records_from_rows",
    "_next_seq",
    "_lock_row",
    "_lock_message",
    "_unlock_message",
    # from checks
    "run_statements",
    "_report_warnings",
    "check_guards",
    "_failed_attempt_problem",
    "_checksum_repair",
    "_is_current",
    "_migration_state",
    "_validation_problems",
    "_changed_since_applied",
    "_changed_down_message",
    # from replay
    "_is_read_savepoint",
    "_ReplayCursor",
    "_ReadStep",
    "SchemaRead",
    "_ReplayConnection",
    # from planning
    "generated_id",
    "plan_migration",
    "drift_lines",
    "render_script",
    # from migrator
    "Migrator",
]


# The old names for the rehearsal row and its key, kept so code written
# against 2.19 and earlier still imports. Deprecated since 2.20.0, removed
# in 3.0.
_RENAMED = {
    "receipt_key": "rehearsal_key",
    "RECEIPT_PASSED": "REHEARSAL_PASSED",
    "RECEIPT_FAILED": "REHEARSAL_FAILED",
    "RECEIPT_OVERRIDE": "REHEARSAL_OVERRIDE",
}


def __getattr__(name: str) -> Any:
    """The renamed names, with a warning naming what to import instead."""
    current = _RENAMED.get(name)
    if current is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"sustained.migrations.{name} is deprecated and will be removed "
        f"in 3.0. Import {current} instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return globals()[current]
