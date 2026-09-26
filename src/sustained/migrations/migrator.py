"""
The Migrator: applies and reverts an ordered list of migrations on one
blocking connection, and keeps the tracking and rehearsal tables.
"""

from __future__ import annotations

import sys
import time
import warnings
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    cast,
)

from sustained.dialects import Dialects
from sustained.driver_errors import is_missing_table
from sustained.execution import (
    cursor_scope,
    enter_autocommit,
    in_transaction,
    pinned_transaction,
    transaction,
)
from sustained.migrations.checks import (
    _changed_down_message,
    _changed_since_applied,
    _checksum_repair,
    _failed_attempt_problem,
    _is_current,
    _migration_state,
    _validation_problems,
    check_guards,
)
from sustained.migrations.migration import (
    AppliedRecord,
    Callbacks,
    Migration,
    MigrationStep,
    _call_on_error,
    _checked_steps,
    _restore_migration,
    _run_step,
    _stored_steps,
    _tag_applied,
    _tag_migration,
    checked_unique_ids,
    migration_checksum,
)
from sustained.migrations.planning import drift_lines, plan_migration, render_script
from sustained.migrations.rehearsal import (
    REHEARSAL_FAILED,
    REHEARSAL_OVERRIDE,
    REHEARSAL_PASSED,
    Rehearsal,
    _check_rehearsable,
    _destructive_in,
    _down_sweep,
    _legacy_rehearsal_key,
    _passed_rehearsal_keys,
    _rehearsal_message,
    _rehearsal_results,
    _rehearsal_writes,
    _reversal_provable,
    _scratch_rehearsal_keys,
    _skipped_results,
    rehearsal_failed,
    rehearsal_key,
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
from sustained.types import Connection, Cursor, SqlValue

if TYPE_CHECKING:
    from sustained.autogenerate import IntrospectedTable
    from sustained.compilers.base import Compiler
    from sustained.guards import Guard, Verdict
    from sustained.introspect import Snapshot
    from sustained.model import Model
    from sustained.schema import TableOptions


class Migrator:
    """
    Applies and reverts an ordered list of migrations on one connection.

    Applied migration ids live in a tracking table, created on first use.
    Each migration runs inside a transaction, so a failing step leaves the
    schema at the previous migration. A migration built with
    transactional=False is the exception: it runs bare, and a failing step
    there leaves the statements before it applied. Engines that do not
    support transactional DDL may still leave partial changes from a
    multi-step migration. Engines without transactions at all, such as Athena, run
    each step bare; a failing migration there can leave partial changes
    that need manual cleanup.

    `guards` are rules that read the statements a run would apply; see
    sustained.guards. A blocking verdict stops up() before any statement
    runs. `callbacks` are the functions to call around a run.
    """

    def __init__(
        self,
        connection: Connection,
        migrations: List[Migration],
        table: str = "sustained_migrations",
        dialect: Dialects = Dialects.DEFAULT,
        tracking_table_options: Optional["TableOptions"] = None,
        rehearsal_table: str = "sustained_rehearsals",
        guards: Optional[Sequence["Guard"]] = None,
        callbacks: Optional[Callbacks] = None,
    ) -> None:
        checked_unique_ids(migrations)
        self._guards = list(guards or [])
        self._callbacks = callbacks or Callbacks()
        self._connection = connection
        self._migrations = list(migrations)
        self._table = table
        self._rehearsal_table = rehearsal_table
        self._dialect = dialect
        self._compiler: "Compiler" = Dialects.get_compiler(dialect)
        self._tracking_table_options = tracking_table_options
        self._tracking_ready = False
        self._rehearsal_ready = False
        self._rehearsing = False

    @property
    def connection(self) -> Connection:
        """The connection this migrator runs on."""
        return self._connection

    @property
    def dialect(self) -> Dialects:
        """The dialect this migrator compiles for."""
        return self._dialect

    @property
    def compiler(self) -> "Compiler":
        """The compiler that renders this migrator's ddl steps."""
        return self._compiler

    def _table_sql(self) -> str:
        return self._compiler.quote_identifier(self._table)

    def _table_ddl_sql(self) -> str:
        return self._compiler.quote_ddl_identifier(self._table)

    def _own_tables(self) -> Tuple[str, ...]:
        """
        The tables Sustained keeps for itself. A diff against the models
        leaves them alone, and a rehearsal snapshot drops them, so its own
        bookkeeping never reads as drift or as an object left behind.
        """
        return (self._table, self._rehearsal_table)

    @contextmanager
    def _migration_scope(self, transactional: bool = True) -> Iterator[None]:
        """
        A transaction on engines whose schema changes roll back; a bare run
        followed by a commit (when the driver has one) on engines whose do
        not.

        `transactional` is the migration's own flag. A migration with
        transactional=False runs outside a transaction on every engine, so
        a statement the engine refuses inside a transaction block, such as
        CREATE INDEX CONCURRENTLY on Postgres, can run. Its tracking row is
        written after its statements, in the same bare mode, so a finished
        migration is still recorded.

        A rehearsal opens one transaction around the whole run and rolls it
        back at the end, so each migration runs bare and nothing commits.

        Nothing takes a failed non-transactional migration back. The
        statements that already ran stay in the database, and the tracking
        row says the attempt failed. The operator finishes or undoes the
        rest by hand and then runs repair(). On Postgres a failed CREATE
        INDEX CONCURRENTLY also leaves an invalid index, which needs a
        DROP INDEX of its own.
        """
        if self._rehearsing:
            yield
            return
        if transactional and self._compiler.supports_transactional_ddl():
            with transaction(self._connection, self._dialect):
                yield
            return
        if not transactional:
            with self._autocommit_scope():
                yield
            return
        yield
        self._commit_quietly()

    @contextmanager
    def _autocommit_scope(self) -> Iterator[None]:
        """
        Runs the block with the driver's own transaction control off, and
        turns it back on at the end. See enter_autocommit().
        """
        restore = enter_autocommit(self._connection)
        try:
            yield
        finally:
            restore()

    def _execute(
        self, cursor: "Cursor", sql: str, params: Tuple[SqlValue, ...]
    ) -> None:
        """Runs one parameterized statement, adapted for the dialect."""
        cursor.execute(*self._compiler.prepare_execution(sql, params))

    def _run_sql(self, sql: str, params: Tuple[SqlValue, ...] = ()) -> None:
        """
        One statement on a cursor of its own, given back when it finishes.
        A cursor left open holds its result set, and pyodbc and the MySQL
        drivers refuse the next statement on the connection once enough of
        those pile up.
        """
        with closing(self._connection.cursor()) as cursor:
            self._execute(cursor, sql, params)

    def _write_tracking_row(self, sql: str, params: Tuple[SqlValue, ...] = ()) -> None:
        """
        One tracking table write inside the migration's own transaction.
        It runs on the transaction's cursor where a block is open, so a
        failed migration takes its row back with it on the engines that
        roll DDL back, and on DuckDB, where every fresh cursor is a session
        of its own.
        """
        with cursor_scope(self._connection) as cursor:
            self._execute(cursor, sql, params)

    def _commit_quietly(self) -> None:
        if hasattr(self._connection, "commit"):
            self._connection.commit()

    def _rollback_quietly(self) -> None:
        try:
            if hasattr(self._connection, "rollback"):
                self._connection.rollback()
        except Exception:
            pass

    def _refuse_open_transaction(self, verb: str) -> None:
        """
        Raises when a transaction() block is open on the connection. The
        run commits its own work as it goes, and that commit would take
        the caller's uncommitted work with it.
        """
        if in_transaction(self._connection):
            raise ValueError(
                f"{verb} cannot run inside an open transaction() block: "
                "it commits as it goes, and the commit would take the "
                "caller's work with it."
            )

    @contextmanager
    def _lock_scope(self) -> Iterator[None]:
        """
        Holds the engine's advisory lock, named after the tracking table,
        for the duration of a run, so concurrent migrators queue instead of
        racing. A no-op on engines without one.
        """
        lock_statements = self._compiler.migration_lock_sql(self._table)
        if not lock_statements:
            yield
            return
        with closing(self._connection.cursor()) as cursor:
            for statement in lock_statements:
                cursor.execute(statement)
                self._check_lock(_lock_row(cursor))
        try:
            yield
        except BaseException:
            # A failed statement outside a transaction() block leaves a
            # Postgres session aborted. The engine then refuses every
            # statement, pg_advisory_unlock included, until a rollback.
            self._rollback_quietly()
            self._release_lock(raising=False)
            raise
        self._release_lock(raising=True)

    def _release_lock(self, raising: bool) -> None:
        """
        Runs the unlock statements. A refused unlock leaves the lock held
        until the connection closes, and every other migrator waits for
        it until then. After a run that succeeded, the refusal raises.
        After a run that failed, it is reported on stderr, so the run's
        own error is the one the caller sees.
        """
        from sustained.exceptions import MigrationError

        failure: Optional[Exception] = None
        for statement in self._compiler.migration_unlock_sql(self._table):
            try:
                with closing(self._connection.cursor()) as cursor:
                    cursor.execute(statement)
            except Exception as error:
                failure = failure or error
        if failure is None:
            return
        message = _unlock_message(self._table, failure)
        if not raising:
            print(f"error: {message}", file=sys.stderr)
            return
        raise MigrationError([message]) from failure

    def _check_lock(self, row: Optional[Sequence[object]]) -> None:
        """
        Raises when the lock statement's result says the lock was not
        granted. MySQL and MSSQL report a refused lock in the value they
        return instead of raising, so a run that read nothing here could
        start while another migrator was working.
        """
        from sustained.exceptions import MigrationError

        problem = self._compiler.migration_lock_problem(row)
        if problem is not None:
            raise MigrationError([_lock_message(self._table, problem)])

    def _ensure_tracking_table(self) -> None:
        from sustained.schema import build_create_table_sql

        if self._tracking_ready:
            return
        sql = build_create_table_sql(
            self._compiler,
            self._table_ddl_sql(),
            _tracking_column_defs(self._compiler.supports_constraints()),
            if_not_exists=True,
            options=self._tracking_table_options,
        )
        with closing(self._connection.cursor()) as cursor:
            cursor.execute(sql)
        self._commit_quietly()
        self._upgrade_tracking_table()
        self._tracking_ready = True

    def _rehearsal_table_sql(self) -> str:
        return self._compiler.quote_identifier(self._rehearsal_table)

    def _rehearsal_table_ddl_sql(self) -> str:
        return self._compiler.quote_ddl_identifier(self._rehearsal_table)

    def _ensure_rehearsal_table(self) -> None:
        from sustained.schema import build_create_table_sql

        if self._rehearsal_ready:
            return
        sql = build_create_table_sql(
            self._compiler,
            self._rehearsal_table_ddl_sql(),
            _rehearsal_column_defs(self._compiler.supports_constraints()),
            if_not_exists=True,
            options=self._tracking_table_options,
        )
        with closing(self._connection.cursor()) as cursor:
            cursor.execute(sql)
        self._commit_quietly()
        self._rehearsal_ready = True

    def record_rehearsal(self, key: str, outcome: str = REHEARSAL_PASSED) -> None:
        """
        Writes the row for one rehearsal key, replacing any earlier row
        for the same key. The outcome is 'passed', 'failed', or 'override'
        for statements applied with unrehearsed=True.

        A rehearsal on a scratch database records nothing on its own: the
        row belongs on the database the next run will read. Pass its
        result to record_scratch_rehearsal() on a migrator bound to that
        database.
        """
        if outcome not in (REHEARSAL_PASSED, REHEARSAL_FAILED, REHEARSAL_OVERRIDE):
            raise ValueError(
                f"Unknown rehearsal outcome {outcome!r}; use "
                f"{REHEARSAL_PASSED!r}, {REHEARSAL_FAILED!r}, or "
                f"{REHEARSAL_OVERRIDE!r}."
            )
        self._refuse_open_transaction("record_rehearsal")
        self._record_rehearsals([key], outcome)

    def _record_rehearsals(
        self, keys: Sequence[str], outcome: str = REHEARSAL_PASSED
    ) -> None:
        """
        Writes one row per key with the same outcome, in one transaction.
        A rehearsal of n pending migrations can prove n * (n + 1) / 2
        prefix keys. For 400 destructive migrations on a local SQLite
        file, a commit per key takes 25 s for the 80,200 rows, where one
        transaction takes 0.4 s.
        """
        self._ensure_rehearsal_table()
        delete_sql, insert_sql, rows = _rehearsal_writes(
            self._compiler, self._rehearsal_table_sql(), keys, outcome
        )
        with closing(self._connection.cursor()) as cursor:
            cursor.executemany(delete_sql, [row[:1] for row in rows])
            cursor.executemany(insert_sql, rows)
        self._commit_quietly()

    def record_scratch_rehearsal(self, results: Rehearsal) -> Optional[str]:
        """
        Writes the rows a passing rehearsal on a scratch database proves,
        on the database this migrator is bound to, and returns the key of
        the full run. Returns None and writes nothing when the rehearsal
        failed, nothing is pending here, or the scratch run did not run
        every migration pending here.

        The scratch database starts from its own schema, so the keys are
        computed against this database's applied history and pending set.
        A row also goes in for each shorter run a `target` would produce
        that removes data, the same rows a rehearsal on this database
        records, so a targeted up() finds its row too.
        """
        keys = _scratch_rehearsal_keys(
            self.applied_records(), self.pending(), results, self._compiler
        )
        if not keys:
            return None
        self._refuse_open_transaction("record_scratch_rehearsal")
        self._record_rehearsals(keys)
        return keys[0]

    def rehearsal_outcome(self, key: str) -> Optional[str]:
        """
        The outcome recorded for this key: 'passed' or 'failed' from a
        rehearsal, 'override' from a run with unrehearsed=True, or None when
        no row covers it.
        """
        self._ensure_rehearsal_table()
        placeholder = self._compiler.placeholder()
        with closing(self._connection.cursor()) as cursor:
            self._execute(
                cursor,
                f"SELECT outcome FROM {self._rehearsal_table_sql()} "
                f"WHERE rehearsal_key = {placeholder}",
                (key,),
            )
            row = cursor.fetchone()
        return None if row is None else str(row[0])

    def rehearsed(self, key: str) -> bool:
        """True when a passing rehearsal covers this key."""
        return self.rehearsal_outcome(key) == REHEARSAL_PASSED

    def run_outcome(
        self, applied: Sequence[AppliedRecord], run: Sequence[Migration]
    ) -> Optional[str]:
        """
        The outcome recorded for a run of these migrations from this
        applied history, as up() reads it. It looks up rehearsal_key()
        first. When no row has that key, it looks up the key a release
        before 2.25.0 wrote for the same run, so a rehearsal recorded
        before an upgrade still covers the run after it.
        """
        outcome = self.rehearsal_outcome(rehearsal_key(applied, run))
        legacy = _legacy_rehearsal_key(applied, run)
        if outcome is None and legacy is not None:
            outcome = self.rehearsal_outcome(legacy)
        return outcome

    def _has_columns(self, columns: Tuple[str, ...]) -> bool:
        """Probes the tracking table for the given columns."""
        try:
            with closing(self._connection.cursor()) as cursor:
                cursor.execute(
                    f"SELECT {quoted_columns(self._compiler, *columns)} "
                    f"FROM {self._table_sql()} WHERE 1 = 0"
                )
                cursor.fetchall()
            return True
        except Exception:
            # A failed probe can poison an open transaction (Postgres
            # aborts it), so clear the slate before the next statement.
            self._rollback_quietly()
            return False

    def _upgrade_tracking_table(self) -> None:
        """
        Brings a tracking table written by an earlier version, which held
        only id and applied_at, up to the current shape. Missing columns
        are added nullable; seq and success are backfilled from the
        existing rows in applied order.
        """
        from sustained.schema import render_column_sql

        if self._has_columns(_UPGRADE_COLUMNS):
            return
        added: List[str] = []
        for name in _UPGRADE_COLUMNS:
            if self._has_columns((name,)):
                continue
            column_sql = render_column_sql(
                self._compiler, name, _upgrade_column_def(name), inline_pk=False
            )
            statement = self._compiler.compile_add_column(
                self._table_ddl_sql(), column_sql
            )
            with closing(self._connection.cursor()) as cursor:
                cursor.execute(statement)
            added.append(name)
        self._commit_quietly()
        placeholder = self._compiler.placeholder()
        # Backfill only the columns this run added, and only where they are
        # still null, so values a partial earlier upgrade wrote survive.
        column = self._compiler.quote_identifier
        if "success" in added:
            self._run_sql(
                f"UPDATE {self._table_sql()} SET {column('success')} = "
                f"{placeholder} WHERE {column('success')} IS NULL",
                (True,),
            )
        if "seq" in added:
            with closing(self._connection.cursor()) as cursor:
                cursor.execute(
                    f"SELECT {column('id')} FROM {self._table_sql()} "
                    f"ORDER BY {quoted_columns(self._compiler, 'applied_at', 'id')}"
                )
                ids = [row[0] for row in cursor.fetchall()]
            for position, migration_id in enumerate(ids, start=1):
                self._run_sql(
                    f"UPDATE {self._table_sql()} SET {column('seq')} = "
                    f"{placeholder} WHERE {column('id')} = {placeholder} "
                    f"AND {column('seq')} IS NULL",
                    (position, migration_id),
                )
        self._commit_quietly()

    def _read_records(self) -> List[AppliedRecord]:
        """Reads the tracking table rows, assuming the table is there."""
        with closing(self._connection.cursor()) as cursor:
            cursor.execute(records_select(self._compiler, self._table_sql()))
            return records_from_rows(cursor.fetchall())

    def applied_records(self) -> List[AppliedRecord]:
        """
        Returns every tracking table row in application order, creating
        the tracking table when it is missing.
        """
        self._ensure_tracking_table()
        return self._read_records()

    def read_applied_records(self) -> List[AppliedRecord]:
        """
        Returns every tracking table row without writing anything.

        An empty list means the run has no history to read: either the
        tracking table does not exist yet, or it has only the columns an
        earlier version wrote. Any other failed read, such as a closed
        connection or a refused SELECT, raises the driver's error, so a
        report never shows every migration pending on a database it could
        not read. The paths that only report on a run, such as script()
        and pending(), read the rows through this, since creating the
        table would change a database they say they leave alone.
        """
        if self._tracking_ready:
            return self._read_records()
        try:
            return self._read_records()
        except Exception as error:
            # A failed read can poison an open transaction, so clear the
            # slate before the next statement.
            self._rollback_quietly()
            if is_missing_table(error) or self._has_earlier_columns():
                return []
            raise

    def _has_earlier_columns(self) -> bool:
        """True when the tracking table lacks the columns added later."""
        return self._has_columns(("id",)) and not self._has_columns(_UPGRADE_COLUMNS)

    def applied(self) -> List[str]:
        """Returns the applied migration ids in application order."""
        return [r.id for r in self.applied_records() if r.success]

    def read_applied(self) -> List[str]:
        """The applied migration ids, without creating the table."""
        return [r.id for r in self.read_applied_records() if r.success]

    def _versioned(self) -> List[Migration]:
        return [m for m in self._migrations if not m.repeatable]

    def _repeatables(self) -> List[Migration]:
        return [m for m in self._migrations if m.repeatable]

    def pending(self) -> List[Migration]:
        """
        Returns the registered migrations the next up() would run:
        versioned migrations without a successful row, then repeatables
        without one or whose checksum changed since the last run.
        """
        records = {r.id: r for r in self.read_applied_records()}
        result = [
            m for m in self._versioned() if not _is_current(records.get(m.id), m, False)
        ]
        result.extend(
            m
            for m in self._repeatables()
            if not _is_current(records.get(m.id), m, True)
        )
        return result

    def status(self) -> List[tuple[str, bool]]:
        """Returns (id, applied) pairs for every registered migration."""
        applied = set(self.read_applied())
        return [(m.id, m.id in applied) for m in self._migrations]

    def statuses(self) -> List[tuple[str, str]]:
        """
        Returns (id, state) pairs for every registered migration. The
        state is 'applied', 'pending', or, for a repeatable whose
        contents changed since its last run, 'changed'.
        """
        records = {r.id: r for r in self.read_applied_records()}
        return [
            (m.id, _migration_state(records.get(m.id), m)) for m in self._migrations
        ]

    def _insert_sql(self) -> str:
        return insert_sql(self._compiler, self._table_sql())

    def _update_sql(self) -> str:
        return update_sql(self._compiler, self._table_sql())

    def validate(self, raise_on_problems: bool = True) -> List[str]:
        """
        Checks the tracking table against the registered migrations and
        returns the problems found: failed attempts, applied migrations
        this migrator does not know, checksum mismatches from edited
        migrations, and out-of-order pending migrations. Raises
        MigrationError when problems exist, unless raise_on_problems is
        False.
        """
        from sustained.exceptions import MigrationError

        problems = _validation_problems(self._migrations, self.read_applied_records())
        if problems and raise_on_problems:
            raise MigrationError(problems)
        return problems

    def repair(self) -> List[str]:
        """
        Brings the tracking table back in line with the registered
        migrations: deletes rows left by failed attempts and rewrites
        stored checksums that no longer match, including null checksums on
        rows written before checksums existed. Returns a description of
        every action taken. Schema changes a failed attempt left behind
        are not touched; clean those up first.

        A changed repeatable keeps its stored checksum. For it a changed
        checksum schedules a re-run, and rewriting the row here would
        cancel that run without the new contents ever reaching the
        database. A repeatable row that stores the checksum format of a
        release before 2.25.0 for unchanged statements is rewritten in the
        current format, like any other row.
        """
        self._refuse_open_transaction("repair")
        records = self.applied_records()
        by_id = {m.id: m for m in self._migrations}
        placeholder = self._compiler.placeholder()
        actions: List[str] = []
        for record in records:
            if not record.success:
                self._run_sql(
                    f"DELETE FROM {self._table_sql()} WHERE "
                    f"{self._compiler.quote_identifier('id')} = {placeholder} "
                    f"AND {self._compiler.quote_identifier('success')} = "
                    f"{self._compiler.compile_boolean(False)}",
                    (record.id,),
                )
                actions.append(f"removed the failed attempt of '{record.id}'")
                continue
            migration = by_id.get(record.id)
            if migration is None:
                continue
            rewrite = _checksum_repair(record, migration)
            if rewrite is not None:
                current, action = rewrite
                self._run_sql(
                    f"UPDATE {self._table_sql()} SET "
                    f"{self._compiler.quote_identifier('checksum')} = "
                    f"{placeholder} WHERE "
                    f"{self._compiler.quote_identifier('id')} = {placeholder}",
                    (current, record.id),
                )
                actions.append(action)
        self._commit_quietly()
        return actions

    def _record_failure(
        self,
        migration: Migration,
        seq: int,
        update: bool = False,
        generated: bool = False,
    ) -> None:
        """
        Writes a failed-attempt row after a migration step raised on an
        engine whose schema changes do not roll back, where partial changes
        may remain. A repeatable that already has a row updates it in
        place. A failure to write the row never masks the original error. A
        rehearsal writes nothing: its whole run rolls back. A migration
        that asked for no transaction leaves partial changes on every
        engine, so it gets a row wherever it fails.
        """
        if self._rehearsing or (
            migration.transactional and self._compiler.supports_transactional_ddl()
        ):
            return
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            checksum = migration_checksum(migration)
            if update:
                self._run_sql(
                    self._update_sql(),
                    (
                        checksum,
                        timestamp,
                        None,
                        False,
                        generated,
                        _stored_steps(migration, generated, self._compiler),
                        migration.id,
                    ),
                )
            else:
                self._run_sql(
                    self._insert_sql(),
                    (
                        migration.id,
                        seq,
                        checksum,
                        timestamp,
                        None,
                        False,
                        generated,
                        _stored_steps(migration, generated, self._compiler),
                    ),
                )
            self._commit_quietly()
        except Exception:
            pass

    def up(
        self,
        target: Optional[str] = None,
        validate: bool = True,
        allow_out_of_order: bool = False,
        *,
        models: Optional[List[Type["Model"]]] = None,
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
        unrehearsed: bool = False,
    ) -> List[str]:
        """
        Applies pending migrations in order, stopping after the target id
        when one is given. Returns the ids that were applied.

        The run validates first: failed attempts, unknown applied ids,
        checksum mismatches, and out-of-order pending migrations all stop
        it. Pass validate=False to skip the checks, or
        allow_out_of_order=True to accept a pending migration that is
        ordered before an applied one.

        Repeatables run after the versioned migrations, whenever their
        checksum is new or changed. A targeted run skips them: a
        repeatable may depend on a versioned migration past the target,
        and the next full up() runs it. The target must name a versioned
        migration.

        With models, the run applies the versioned migrations first, then
        diffs the models against the database and applies the generated
        migration, then the repeatables. The diff is taken after the
        pending migrations have run, so it sees the schema they left and
        never regenerates a table one of them just created. Additive
        changes generate reversible steps, so down() takes them back.
        Drops need allow_drops=True and do not reverse. A generated
        migration always runs last of the versioned ones, so it cannot be
        combined with a target, and the remaining arguments are the diff
        options plan() takes.

        Everything after allow_out_of_order is keyword-only, so a call
        written for an earlier release cannot bind a diff option to a
        positional argument it never meant.

        A run that would remove data stops unless a passing rehearsal
        covers exactly these statements against exactly this applied
        history. Rehearse first, or pass unrehearsed=True to apply them
        without the proof, which writes an 'override' row naming what
        was applied unproved. Runs that only add are never gated, and a
        callable step is invisible to the check, the same limit the
        destructive labels carry.

        The migrator's guards read the statements before they run. A
        blocking verdict raises GuardBlocked; a warning prints on stderr.
        Both gates read the registered migrations before anything runs,
        and read them again together with the generated migration, whose
        statements exist only once the registered ones have applied. A
        block or a missing row at that second reading leaves the
        registered migrations applied. Any error raised after a migration
        applied lists the ids that applied on the exception's `applied`
        attribute. The migrator's callbacks fire around the run.
        """
        self._refuse_open_transaction("up")
        callbacks = self._callbacks
        if callbacks.before_migrate is not None:
            callbacks.before_migrate(self._connection)
        try:
            applied = self._run_up(
                target=target,
                validate=validate,
                allow_out_of_order=allow_out_of_order,
                models=models,
                allow_drops=allow_drops,
                ignore_changed_columns=ignore_changed_columns,
                migration_id=migration_id,
                renames=renames,
                table_renames=table_renames,
                type_casts=type_casts,
                unrehearsed=unrehearsed,
            )
        except Exception as error:
            _call_on_error(callbacks, self._connection, error)
            raise
        if applied and callbacks.after_migrate is not None:
            callbacks.after_migrate(self._connection, applied)
        return applied

    def _run_up(
        self,
        target: Optional[str],
        validate: bool,
        allow_out_of_order: bool,
        models: Optional[List[Type["Model"]]],
        allow_drops: bool,
        ignore_changed_columns: bool,
        migration_id: Optional[str],
        renames: Optional[dict[str, str]],
        table_renames: Optional[dict[str, str]],
        type_casts: Optional[dict[str, str]],
        unrehearsed: bool,
    ) -> List[str]:
        """The run itself, without the callbacks up() wraps it in."""
        from sustained.exceptions import MigrationError

        if models is not None and target is not None:
            raise ValueError(
                "up() cannot take both models and a target: the generated "
                "migration always runs last, so a target would leave it out."
            )
        require_registered = models is None

        with self._lock_scope():
            if models is not None:
                self._ensure_tracking_table()

            migrations = self._versioned()
            if target is not None:
                ids = [m.id for m in migrations]
                if target not in ids:
                    if any(m.id == target for m in self._repeatables()):
                        raise ValueError(
                            f"Migration target {target!r} is repeatable; a "
                            "target must name a versioned migration."
                        )
                    raise ValueError(f"Unknown migration target: {target!r}.")
                migrations = migrations[: ids.index(target) + 1]

            records = self.applied_records()
            if validate:
                problems = _validation_problems(
                    self._migrations,
                    records,
                    allow_out_of_order,
                    require_registered=require_registered,
                )
                if problems:
                    raise MigrationError(problems)
            records_by_id = {r.id: r for r in records}
            already_applied = {r.id for r in records if r.success}
            next_seq = _next_seq(records)
            applied_now: List[str] = []
            versioned_now = [m for m in migrations if m.id not in already_applied]
            repeatables_now = [
                m
                for m in (self._repeatables() if target is None else [])
                if not _is_current(records_by_id.get(m.id), m, True)
            ]
            # The registered set is checked before anything runs. The
            # order matches pending(), so a rehearsal of the same set
            # produces the same key.
            registered_run = versioned_now + repeatables_now
            warned: Set["Verdict"] = set()
            check_guards(self._guards, registered_run, self._dialect, warned)
            self._require_rehearsal_row(records, registered_run, unrehearsed, target)
            final_run = list(registered_run)
            # A migration applied before a failure stays applied and
            # committed, so the error lists it for the caller.
            try:
                for migration in versioned_now:
                    self._apply(migration, next_seq, update=False)
                    next_seq += 1
                    applied_now.append(migration.id)
                if models is not None:
                    generated = self.plan(
                        models,
                        allow_drops=allow_drops,
                        ignore_changed_columns=ignore_changed_columns,
                        migration_id=migration_id,
                        renames=renames,
                        table_renames=table_renames,
                        type_casts=type_casts,
                    )
                    if generated is not None:
                        # The generated statements are known only now, after
                        # the registered migrations left the schema they diff
                        # against, so both gates run a second time before the
                        # one migration they could not see. The registered
                        # migrations are already applied and committed by
                        # then, so a block here reports what it stopped after.
                        final_run = registered_run + [generated]
                        check_guards(self._guards, final_run, self._dialect, warned)
                        self._require_rehearsal_row(
                            records, final_run, unrehearsed, target
                        )
                        # The migration joins the registered list only after
                        # it applied. A failed one left there would run again
                        # on the next up() of a long-lived migrator, and would
                        # run alongside a fresh diff of the same models.
                        self._apply(generated, next_seq, update=False, generated=True)
                        self._migrations.append(generated)
                        next_seq += 1
                        applied_now.append(generated.id)
                for migration in repeatables_now:
                    record = records_by_id.get(migration.id)
                    self._apply(migration, next_seq, update=record is not None)
                    if record is None:
                        next_seq += 1
                    applied_now.append(migration.id)
                if unrehearsed and _destructive_in(final_run, self._compiler):
                    # The proof was waived, so the row says so. It never
                    # unlocks a later run: only 'passed' does that.
                    self.record_rehearsal(
                        rehearsal_key(records, final_run), REHEARSAL_OVERRIDE
                    )
                return applied_now
            except Exception as error:
                _tag_applied(error, applied_now)
                raise

    def _require_rehearsal_row(
        self,
        records: List[AppliedRecord],
        run: List[Migration],
        unrehearsed: bool,
        target: Optional[str] = None,
    ) -> None:
        """
        Stops a run that removes data unless a passing rehearsal covers
        exactly this content. A run that only adds passes straight
        through and never reads the rehearsal table.
        """
        from sustained.exceptions import RehearsalRequired

        if unrehearsed:
            return
        destructive = _destructive_in(run, self._compiler)
        if not destructive:
            return
        outcome = self.run_outcome(records, run)
        if outcome == REHEARSAL_PASSED:
            return
        raise RehearsalRequired(_rehearsal_message(destructive, outcome, target))

    def _apply(
        self, migration: Migration, seq: int, update: bool, generated: bool = False
    ) -> None:
        """
        Runs one migration's up step and records it: an INSERT for a
        first run, an UPDATE in place when a repeatable re-runs, keeping
        its original seq. `generated` marks a migration the diff against
        the models produced, whose id nothing on disk carries.
        """
        try:
            with self._migration_scope(migration.transactional):
                started = time.perf_counter()
                _run_step(self._connection, migration.up, self._compiler)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                timestamp = datetime.now(timezone.utc).isoformat()
                checksum = migration_checksum(migration)
                if update:
                    self._write_tracking_row(
                        self._update_sql(),
                        (
                            checksum,
                            timestamp,
                            elapsed_ms,
                            True,
                            generated,
                            _stored_steps(migration, generated, self._compiler),
                            migration.id,
                        ),
                    )
                else:
                    self._write_tracking_row(
                        self._insert_sql(),
                        (
                            migration.id,
                            seq,
                            checksum,
                            timestamp,
                            elapsed_ms,
                            True,
                            generated,
                            _stored_steps(migration, generated, self._compiler),
                        ),
                    )
        except Exception as error:
            self._record_failure(migration, seq, update=update, generated=generated)
            _tag_migration(error, migration.id)
            raise

    def rehearse(
        self,
        scratch: bool = False,
        models: Optional[List[Type["Model"]]] = None,
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
    ) -> Rehearsal:
        """
        Runs every pending migration up, then back down, inside one
        transaction, and rolls that transaction back. Returns one result
        per migration that ran; an empty result means nothing was pending.

        A rehearsal proves that the SQL is valid, that the schema moved,
        and that the down steps take it back. It does not prove anything
        about data on a production-sized table. The tracking table is
        created if it does not exist yet; nothing else survives. A
        callable step that commits on its own is the exception: that
        commit cannot be taken back.

        With models, the run rehearses what up(models=[...]) would apply:
        the generated migration joins the pending list for this run only,
        and its result reports whether the schema then matched the models.
        The remaining arguments are the diff options up() takes, and they
        should match the ones the real run will use.

        The up steps run in the order up() runs them: the versioned
        migrations, then the generated one, then the repeatables. The down
        steps then run newest-first, skipping the repeatables, which have
        none. The first migration without a down step stops the sweep,
        since everything older sits under changes that cannot be taken
        back. The first step that raises stops the rehearsal.

        The schema is read before the run and again after the down sweep.
        A difference between the two means a down step ran without taking
        its change back. The comparison is only made when every step in
        the run reversed: one migration without a down step leaves changes
        that no other step can be blamed for. Tables and columns are
        compared; indexes, constraints, and defaults are not, so a
        leftover index is not reported yet.

        A passing run leaves a rehearsal row behind, keyed to the applied
        history it started from and the statements it ran, which up() reads
        before it applies anything that removes data. A failing run records
        the failure under the same key. A scratch rehearsal records
        nothing, since the row belongs on the database the next run
        will read; the key comes back on the result for the caller to
        record there.

        Only dialects whose schema changes roll back may rehearse. Pass
        scratch=True when the connection points at a database that can be
        thrown away: the dialect check is then skipped, and the changes may
        survive the rollback.

        A migration with transactional=False is left out of the run: its
        statements refuse or ignore a transaction block, and the rehearsal
        runs inside one. Its result reports up_ok as None with the reason,
        the run can still pass, and the row a passing run records covers
        it without proof. What such a migration does can only be seen by
        running it.
        """
        if not scratch:
            _check_rehearsable(self._dialect)
        if getattr(self._connection, "autocommit", False) is True:
            raise ValueError(
                "rehearse cannot run on a connection in autocommit mode: "
                "nothing would roll back. Open the connection without "
                "autocommit, or point rehearse at a scratch database."
            )
        if in_transaction(self._connection):
            raise ValueError(
                "rehearse cannot run inside an open transaction() block: "
                "its rollback would take the caller's work back too."
            )
        # The lock sits outside the rehearsal transaction, so the rollback
        # runs before the lock is released. The state reads sit inside it,
        # so a concurrent migrator cannot apply between the read and the
        # rehearsal.
        with self._lock_scope():
            self.validate()
            pending = self.pending()
            record_list = self.applied_records()
            if not pending and models is None:
                return Rehearsal([], rehearsal_key(record_list, []))
            if models is not None:
                self._ensure_tracking_table()
            records = {r.id: r for r in record_list}
            seq = _next_seq(record_list)
            before = self._snapshot()
            # What the row will cover: the pending set, plus the
            # generated migration once the diff produces one.
            attempted: List[Migration] = list(pending)
            has_drift = False
            # Close whatever transaction the reads above opened, so the
            # rehearsal's BEGIN starts a fresh one instead of warning.
            self._rollback_quietly()
            self._rehearsing = True
            # The rehearsal's statements share this transaction's cursor,
            # so on an engine that gives every cursor its own session they
            # land in the transaction the rollback below takes back.
            with pinned_transaction(self._connection, self._dialect):
                try:
                    ran: List[Migration] = []
                    skipped: List[Migration] = []
                    up_error: Optional[Tuple[str, str]] = None

                    def apply_each(group: List[Migration]) -> None:
                        nonlocal seq, up_error
                        for migration in group:
                            if not migration.transactional:
                                # The rehearsal runs inside one transaction,
                                # which this migration's statements refuse or
                                # ignore. It is reported as unproved rather
                                # than run and failed.
                                skipped.append(migration)
                                continue
                            try:
                                self._apply(
                                    migration, seq, update=migration.id in records
                                )
                            except Exception as error:
                                up_error = (migration.id, str(error))
                                return
                            seq += 1
                            ran.append(migration)

                    # The order matches up(): the versioned migrations, then
                    # the generated one, then the repeatables, which may read
                    # objects the generated migration creates.
                    apply_each([m for m in pending if not m.repeatable])
                    landed: Dict[str, List[str]] = {}
                    if models is not None and up_error is None:
                        # The diff is taken here, inside the rehearsal, so it
                        # sees the schema the pending migrations just left. The
                        # generated migration joins the run without being
                        # registered: nothing outside the rehearsal should see a
                        # migration the rollback is about to take back.
                        drift = self.plan(
                            models,
                            allow_drops=allow_drops,
                            ignore_changed_columns=ignore_changed_columns,
                            migration_id=migration_id,
                            renames=renames,
                            table_renames=table_renames,
                            type_casts=type_casts,
                        )
                        if drift is not None:
                            attempted.append(drift)
                            has_drift = True
                            if not drift.transactional:
                                # A generated SQLite rebuild says
                                # transactional=False, and its pragmas are
                                # ignored inside the rehearsal transaction.
                                skipped.append(drift)
                            else:
                                try:
                                    self._apply(
                                        drift, seq, update=False, generated=True
                                    )
                                except Exception as error:
                                    up_error = (drift.id, str(error))
                                else:
                                    seq += 1
                                    ran.append(drift)
                                    # The renames have already run, so the
                                    # schema holds the new names. Passing the
                                    # hints again would ask to rename objects
                                    # that are gone.
                                    landed[drift.id] = self.drift(
                                        models,
                                        ignore_changed_columns=ignore_changed_columns,
                                    )
                    if up_error is None:
                        apply_each([m for m in pending if m.repeatable])
                    outcomes = {} if up_error else self._rehearse_down(ran)
                    reverted = None
                    if before is not None and _reversal_provable(ran, outcomes):
                        from sustained.autogenerate import diff_snapshots

                        after = self._snapshot()
                        if after is not None:
                            reverted = diff_snapshots(before, after)
                    results = _rehearsal_results(
                        ran, up_error, outcomes, landed, reverted
                    ) + _skipped_results(skipped)
                finally:
                    self._rehearsing = False
                    self._roll_back_rehearsal()
            # The rehearsal row is written after the rollback, in its own
            # committed transaction, and still inside the lock: everything
            # the rehearsal itself wrote has just been taken back.
            key = rehearsal_key(record_list, attempted)
            passed = not any(rehearsal_failed(r) for r in results)
            recorded = False
            if not scratch:
                if passed:
                    self._record_rehearsals(
                        _passed_rehearsal_keys(
                            record_list, pending, key, has_drift, self._compiler
                        )
                    )
                else:
                    self._record_rehearsals([key], REHEARSAL_FAILED)
                recorded = True
            return Rehearsal(results, key, recorded)

    def _snapshot(self) -> Optional[Dict[str, "IntrospectedTable"]]:
        """
        The live schema, without Sustained's own tables, or None when the
        database will not report it. A rehearsal compares two of these,
        and the tracking and rehearsal tables are created by the rehearsal
        itself, so leaving them in would report them as objects left
        behind.

        A read that raises leaves the rehearsal's other proofs standing
        and reports the comparison as not checked, which is what a
        scratch database on an engine Sustained cannot introspect gives.
        """
        from sustained.autogenerate import introspect_schema

        try:
            schema = introspect_schema(self._connection, self._dialect)
        except Exception:
            return None
        for name in self._own_tables():
            schema.pop(name.lower(), None)
        return dict(schema)

    def drift(
        self,
        models: List[Type["Model"]],
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        ignore_changed_columns: bool = False,
    ) -> List[str]:
        """
        What the models still ask for, one readable line each, empty when
        the database holds everything they declare.

        Objects the database holds and the models do not are left out. A
        generated migration leaves those alone unless drops are allowed,
        so a schema built partly by hand does not read as drift here. Use
        plan() for the full comparison, drops included.

        Pass ignore_changed_columns=True to leave type and nullability
        changes out, matching a run that generates its migration the same
        way.
        """
        return drift_lines(
            self._connection,
            models,
            self._dialect,
            self._own_tables(),
            renames=renames,
            table_renames=table_renames,
            ignore_changed_columns=ignore_changed_columns,
        )

    def _roll_back_rehearsal(self) -> None:
        """
        Takes back everything the rehearsal did. The statement runs first,
        on the rehearsal's own cursor, because a driver's own rollback()
        call does nothing on connections that never opened a transaction of
        their own; the driver call follows to leave its bookkeeping
        straight.
        """
        statement = self._compiler.rollback_transaction_sql()
        if statement is not None:
            try:
                with cursor_scope(self._connection) as cursor:
                    cursor.execute(statement)
            except Exception:
                pass
        self._rollback_quietly()

    def _rehearse_down(
        self, ran: List[Migration]
    ) -> Dict[str, Tuple[Optional[bool], Optional[str]]]:
        """
        Runs the down steps of a rehearsal, newest-first, and reports what
        each one proved. A step that raises stops the sweep; the
        migrations under it report that they were not reached.
        """
        placeholder = self._compiler.placeholder()
        outcomes: Dict[str, Tuple[Optional[bool], Optional[str]]] = {}
        failed: Optional[str] = None
        for migration, reason in _down_sweep(ran):
            if failed is not None:
                outcomes[migration.id] = (
                    None,
                    f"down not reached: '{failed}' down failed",
                )
            elif reason is not None:
                outcomes[migration.id] = (None, reason)
            else:
                try:
                    with self._migration_scope(migration.transactional):
                        _run_step(
                            self._connection,
                            cast(MigrationStep, migration.down),
                            self._compiler,
                        )
                        self._run_sql(
                            f"DELETE FROM {self._table_sql()} WHERE "
                            f"{self._compiler.quote_identifier('id')} = "
                            f"{placeholder}",
                            (migration.id,),
                        )
                except Exception as error:
                    outcomes[migration.id] = (False, str(error))
                    failed = migration.id
                else:
                    outcomes[migration.id] = (True, None)
        return outcomes

    def baseline(self, target: str) -> List[str]:
        """
        Marks registered migrations up to and including the target as
        applied without running them, for adopting a database whose schema
        already matches. Rows are written with real checksums and a null
        execution time; already-applied migrations are skipped. Returns the
        ids that were recorded.

        The target must name a versioned migration. Every repeatable is
        recorded at its current checksum, so the first migrate after
        adoption does not re-run objects the schema already holds.

        Raises MigrationError before it writes a row when a migration it
        would record has a failed attempt on record; run repair() first.
        A failure part way rolls back every row this call inserted.
        """
        from sustained.exceptions import MigrationError

        self._refuse_open_transaction("baseline")
        versioned = self._versioned()
        ids = [m.id for m in versioned]
        if target not in ids:
            if any(m.id == target for m in self._repeatables()):
                raise ValueError(
                    f"Migration target {target!r} is repeatable; a target "
                    "must name a versioned migration."
                )
            raise ValueError(f"Unknown migration target: {target!r}.")
        with self._lock_scope():
            records = self.applied_records()
            already_applied = {r.id for r in records if r.success}
            candidates = versioned[: ids.index(target) + 1] + self._repeatables()
            # A failed row keeps the id, so a second row for it breaks the
            # table's primary key part way through the run.
            failed_ids = {r.id for r in records if not r.success}
            failed = [
                _failed_attempt_problem(m.id) for m in candidates if m.id in failed_ids
            ]
            if failed:
                raise MigrationError(failed)
            next_seq = _next_seq(records)
            recorded: List[str] = []
            try:
                for migration in candidates:
                    if migration.id in already_applied:
                        continue
                    timestamp = datetime.now(timezone.utc).isoformat()
                    self._run_sql(
                        self._insert_sql(),
                        (
                            migration.id,
                            next_seq,
                            migration_checksum(migration),
                            timestamp,
                            None,
                            True,
                            False,
                            None,
                        ),
                    )
                    next_seq += 1
                    recorded.append(migration.id)
                self._commit_quietly()
            except BaseException:
                # Without an advisory lock no scope rolls back, and rows
                # already inserted would be written by the next commit.
                self._rollback_quietly()
                raise
            return recorded

    def plan(
        self,
        models: List[Type["Model"]],
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
        ignore_undeclared: bool = True,
        snapshot: Optional["Snapshot"] = None,
    ) -> Optional[Migration]:
        """
        Diffs the database against the models and returns the migration
        up(models=[...]) would generate, without registering or applying
        it. Returns None when the schema is already up to date. The
        tracking table is excluded from the diff.

        Objects the models do not declare are left alone, since a
        database may hold tables that hand-written migrations created.
        Pass allow_drops=True to generate the drops instead, or
        ignore_undeclared=False to refuse to generate while they exist.

        Pass a snapshot from read_schema() to plan against it instead of
        reading the schema again. The snapshot is not changed, so one
        read can feed several plans.
        """
        return plan_migration(
            self._connection,
            models,
            self._dialect,
            self._own_tables(),
            allow_drops=allow_drops,
            ignore_changed_columns=ignore_changed_columns,
            migration_id=migration_id,
            renames=renames,
            table_renames=table_renames,
            type_casts=type_casts,
            ignore_undeclared=ignore_undeclared,
            snapshot=snapshot,
        )

    def read_schema(self, models: List[Type["Model"]]) -> "Snapshot":
        """
        Reads the schema plan() diffs the models against: the
        connection's own schema plus every schema the models name.
        """
        from sustained.autogenerate import declared_schemas, introspect_schema

        return introspect_schema(
            self._connection, self._dialect, declared_schemas(models)
        )

    def sync(
        self,
        models: List[Type["Model"]],
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
    ) -> List[str]:
        """
        Deprecated since 2.13.0, removed in 3.0: call
        up(models=[...]) instead, which does the same work under the verb
        the CLI and the docs already use.
        """
        warnings.warn(
            "Migrator.sync() is deprecated and will be removed in 3.0. "
            "Call up(models=[...]) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.up(
            models=models,
            allow_drops=allow_drops,
            ignore_changed_columns=ignore_changed_columns,
            migration_id=migration_id,
            renames=renames,
            table_renames=table_renames,
            type_casts=type_casts,
        )

    def script(self, direction: str = "up") -> str:
        """
        Renders the SQL a run would execute, without executing anything,
        for review or DBA handoff. 'up' renders every pending migration;
        'down' renders the applied migrations newest-first. Tracking table
        bookkeeping statements are included.

        Nothing is written, not even the tracking table: a database
        without one reads as a database with no migrations applied. A
        migration generated from the models renders its down step from
        the statements its tracking row stores, as down() reverts it.
        """
        records = self.read_applied_records()
        generated: Dict[str, Migration] = {}
        if direction == "down":
            registered = {m.id for m in self._migrations}
            for record in records:
                if record.generated and record.id not in registered:
                    restored = self._generated_migration(record.id)
                    if restored is not None:
                        generated[record.id] = restored
        return render_script(
            self._compiler,
            self._table_sql(),
            self._migrations,
            records,
            direction,
            generated,
        )

    def _applied_versioned(self, ids: Optional[List[str]] = None) -> List[str]:
        """
        Applied ids with the repeatables left out; down() skips them. The
        ids are read from the tracking table unless the caller passes a
        list it has already read.
        """
        repeatable_ids = {m.id for m in self._repeatables()}
        source = self.applied() if ids is None else ids
        return [i for i in source if i not in repeatable_ids]

    def down_to(self, target: str, allow_changed: bool = False) -> List[str]:
        """
        Reverts applied migrations newest-first until the target is the
        most recent applied migration. The target itself stays applied.
        Repeatables are never reverted. `allow_changed` is passed to
        down().
        """
        self._refuse_open_transaction("down_to")
        applied = self._applied_versioned()
        if target not in applied:
            raise ValueError(f"Migration '{target}' is not applied.")
        steps = len(applied) - applied.index(target) - 1
        return self.down(steps, allow_changed=allow_changed) if steps else []

    def _generated_migration(self, migration_id: str) -> Optional[Migration]:
        """
        The migration a generated tracking row describes, read back from
        the row itself, or None when the row holds no statements.

        up(models=[...]) applies a migration that exists nowhere but that
        run, so a later process has nothing to revert it with. The row
        carries the statements for exactly that reason.
        """
        placeholder = self._compiler.placeholder()
        with closing(self._connection.cursor()) as cursor:
            self._execute(
                cursor,
                f"SELECT {self._compiler.quote_identifier('steps')} "
                f"FROM {self._table_sql()} WHERE "
                f"{self._compiler.quote_identifier('id')} = {placeholder}",
                (migration_id,),
            )
            row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return _restore_migration(migration_id, str(row[0]))

    def down(self, steps: int = 1, allow_changed: bool = False) -> List[str]:
        """
        Reverts the most recently applied migrations, newest first. Every
        reverted migration must define a down step. Repeatables are never
        reverted. Returns the ids that were reverted.

        A migration generated from the models is reverted from its own
        tracking row, which holds the statements it ran, so a process that
        never saw the diff can still take it back. Every other migration
        must be registered with this migrator.

        `steps` counts migrations and must be 0 or more. A count of 0
        reverts nothing and returns an empty list.

        A migration whose statements changed since it was applied raises
        MigrationError, because its down step describes the new contents
        and the database holds the old ones. Pass allow_changed=True to
        revert it with the down step as it stands now.

        Every migration in the window is read and checked first, so a
        refusal reverts nothing. A failed attempt on record refuses the
        run, because the window would skip that migration and revert the
        one before it.

        A down step that fails where nothing rolls it back, on an engine
        without transactional DDL or in a migration with
        transactional=False, marks the migration's row failed. Validation
        then blocks the next run until the revert is finished by hand and
        repair() removes the row. The migrator's on_error callback fires
        for a failed run.
        """
        _checked_steps(steps)
        self._refuse_open_transaction("down")
        try:
            return self._run_down(steps, allow_changed)
        except Exception as error:
            _call_on_error(self._callbacks, self._connection, error)
            raise

    def _run_down(self, steps: int, allow_changed: bool) -> List[str]:
        """The run itself, without the callback down() wraps it in."""
        from sustained.exceptions import MigrationError

        with self._lock_scope():
            self._ensure_tracking_table()
            records = self._read_records()
            failed = [_failed_attempt_problem(r.id) for r in records if not r.success]
            if failed:
                raise MigrationError(failed)
            by_record = {r.id: r for r in records}
            applied = self._applied_versioned([r.id for r in records if r.success])
            by_id = {m.id: m for m in self._migrations}
            placeholder = self._compiler.placeholder()
            reverted: List[str] = []
            # Every migration in the window is read and checked before the
            # first one is reverted. A refusal in the middle of the loop
            # would leave the newer migrations reverted and committed for a
            # condition that was knowable before any of them ran.
            window: List[Tuple[str, Migration, MigrationStep]] = []
            for migration_id in reversed(applied[-steps:] if steps else []):
                migration = by_id.get(migration_id)
                if migration is not None and not allow_changed:
                    if _changed_since_applied(migration, by_record.get(migration_id)):
                        raise MigrationError([_changed_down_message(migration_id)])
                if migration is None:
                    migration = self._generated_migration(migration_id)
                if migration is None:
                    raise ValueError(
                        f"Applied migration '{migration_id}' is not registered "
                        "with this migrator; cannot revert."
                    )
                if migration.down is None:
                    raise ValueError(f"Migration '{migration_id}' has no down step.")
                window.append((migration_id, migration, migration.down))
            for migration_id, migration, down_step in window:
                try:
                    with self._migration_scope(migration.transactional):
                        _run_step(self._connection, down_step, self._compiler)
                        self._write_tracking_row(
                            f"DELETE FROM {self._table_sql()} WHERE "
                            f"{self._compiler.quote_identifier('id')} = "
                            f"{placeholder}",
                            (migration_id,),
                        )
                except Exception as error:
                    self._record_down_failure(migration)
                    _tag_migration(error, migration_id)
                    raise
                reverted.append(migration_id)
            return reverted

    def _record_down_failure(self, migration: Migration) -> None:
        """
        Marks a migration's row failed after its down step raised where
        nothing rolled the step back, so partial changes may remain. The
        row keeps its checksum and stored steps. A failure to write the
        mark never masks the original error, and a transaction that rolled
        the step back leaves the row as it was.
        """
        if self._rehearsing or (
            migration.transactional and self._compiler.supports_transactional_ddl()
        ):
            return
        column = self._compiler.quote_identifier
        placeholder = self._compiler.placeholder()
        try:
            self._run_sql(
                f"UPDATE {self._table_sql()} SET {column('success')} = "
                f"{placeholder} WHERE {column('id')} = {placeholder}",
                (False, migration.id),
            )
            self._commit_quietly()
        except Exception:
            pass
