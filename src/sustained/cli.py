"""
Command-line migration runner.

`sustained <command> --config <module>` drives a Migrator from the shell.
The config module names the pieces the migrator needs:

- `connection` (a DB-API connection) or `get_connection()` returning one
- `migrations`: a list of Migration objects, and/or `migrations_dir`: a
  directory of `<id>.up.sql` / `<id>.down.sql` / `<id>.repeat.sql` files
  loaded after the list
- `placeholders`: a dict filling `${key}` markers in the SQL files
  (optional)
- `models`: a list of Model classes, which lets `plan` report drift
  between the models and the database (optional)
- `dialect`: a Dialects member or its name, such as 'postgres' (optional)
- `table`: the tracking table name (optional)
- `rehearsal_table`: the rehearsal table name (optional)
- `tracking_table_options`: TableOptions for the tracking table (optional)
- `get_rehearsal_connection()`: a connection to a scratch database, which
  `rehearse` then uses instead of the real one (optional)
- `guards`: a list of rules over the statements a run would apply; see
  sustained.guards (optional)
- `before_migrate(connection)`, `after_migrate(connection, applied)`, and
  `on_error(connection, migration_id, error)`: callbacks around the
  `migrate` command (optional)

Commands: status, plan, migrate, rehearse, down, validate, repair,
script, baseline. Every command exits 0 on success and 1 on failure.
`plan` exits 2 when work is waiting. `plan` and `migrate` exit 3 when a
guard blocked a statement. A run with problems exits 1 even when a guard
also blocked: a plan that cannot be trusted outranks the rest.

`migrate` refuses to apply statements that remove data until a passing
rehearsal has covered them, and exits 4. `--unrehearsed` applies them
anyway and records the override on the database.

`status`, `validate`, `plan`, and `rehearse` take `--json`, which prints
one JSON object instead of the plain lines. The exit code stays the same
either way.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from types import ModuleType
from typing import (
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from sustained.analysis import (
    MigrationStatement,
    PendingSummary,
    destructive_statements,
    normalize_statement,
    summarize,
)
from sustained.dialects import Dialects
from sustained.exceptions import GuardBlocked, MigrationError, RehearsalRequired
from sustained.guards import Verdict, blocking, run_guards
from sustained.migration_files import load_migrations
from sustained.migrations import (
    CallbackResult,
    Callbacks,
    Migration,
    Migrator,
    Rehearsal,
    RehearsalResult,
    _destructive_prefix_keys,
    migration_sql,
    rehearsal_failed,
    rehearsal_key,
)
from sustained.types import Connection

JsonValue = Union[
    str, int, float, bool, None, Sequence["JsonValue"], Mapping[str, "JsonValue"]
]
"""Anything --json prints: what json.dumps accepts, and nothing else."""


def _resolve_dialect(value: object) -> Dialects:
    if value is None:
        return Dialects.DEFAULT
    if isinstance(value, Dialects):
        return value
    name = str(value).upper()
    try:
        return Dialects[name]
    except KeyError:
        names = ", ".join(d.name.lower() for d in Dialects)
        raise ValueError(f"Unknown dialect {value!r}. Choose one of: {names}.")


def _load_config(module_name: str) -> ModuleType:
    """
    Imports the config module with the working directory on sys.path.

    The entry is removed again by value, not by position. A config module
    may add its own entries to the front of sys.path while it imports, and
    removing the first entry would drop one of those and leave the working
    directory in place for the rest of the process.
    """
    cwd = os.getcwd()
    sys.path.insert(0, cwd)
    try:
        return importlib.import_module(module_name)
    finally:
        try:
            sys.path.remove(cwd)
        except ValueError:
            pass


def _close_quietly(connection: object) -> None:
    if hasattr(connection, "close"):
        try:
            connection.close()
        except Exception:
            pass


def _migrator_on(connection: Connection, config: ModuleType) -> Migrator:
    """Builds a migrator for the config module on the given connection."""
    migrations: List[Migration] = list(getattr(config, "migrations", []))
    directory = getattr(config, "migrations_dir", None)
    if directory is not None:
        migrations.extend(
            load_migrations(
                directory, placeholders=getattr(config, "placeholders", None)
            )
        )
    return Migrator(
        connection,
        migrations,
        table=getattr(config, "table", "sustained_migrations"),
        rehearsal_table=getattr(config, "rehearsal_table", "sustained_rehearsals"),
        dialect=_resolve_dialect(getattr(config, "dialect", None)),
        tracking_table_options=getattr(config, "tracking_table_options", None),
        guards=list(getattr(config, "guards", None) or []),
        callbacks=_config_callbacks(config),
    )


def _callback(config: ModuleType, name: str) -> Optional[Callable[..., CallbackResult]]:
    """The named callback from the config module, or None when it has none."""
    hook = getattr(config, name, None)
    return hook if callable(hook) else None


def _config_callbacks(config: ModuleType) -> Callbacks:
    """
    The config module's callbacks, in the shape the migrator takes. The
    module is how the CLI gathers them; the migrator is what calls them.
    """
    return Callbacks(
        before_migrate=_callback(config, "before_migrate"),
        after_migrate=_callback(config, "after_migrate"),
        on_error=_callback(config, "on_error"),
    )


def _build_migrator(config: ModuleType) -> Tuple[Migrator, Connection]:
    if hasattr(config, "connection"):
        connection = config.connection
    elif hasattr(config, "get_connection"):
        connection = config.get_connection()
    else:
        raise ValueError(
            "The config module must define 'connection' or 'get_connection()'."
        )
    try:
        migrator = _migrator_on(connection, config)
    except Exception:
        # The connection never reaches the caller on a setup failure, so it
        # must close here.
        _close_quietly(connection)
        raise
    return migrator, connection


def _print_json(payload: JsonValue) -> None:
    print(json.dumps(payload, indent=2))


def _cmd_status(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    states = migrator.statuses()
    if args.json:
        _print_json(
            {
                "migrations": [
                    {"id": migration_id, "state": state}
                    for migration_id, state in states
                ]
            }
        )
        return 0
    for migration_id, state in states:
        print(f"{state:8} {migration_id}")
    return 0


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _drift_statements(migrator: Migrator, config: ModuleType) -> Optional[List[str]]:
    """
    The statements that would close the gap between the config module's
    models and the database, or None when the module names no models.

    Drops are included: a preview reports every difference, including
    tables and columns the models no longer declare, which migrate does
    not generate. The statements print in full, so a drop reads as a drop
    without a separate label.
    """
    models = getattr(config, "models", None)
    if not models:
        return None
    migration = migrator.plan(list(models), allow_drops=True)
    if migration is None:
        return []
    return migration_sql(migration, "up", migrator.compiler)


def _migrate_drift_statements(
    migrator: Migrator, config: ModuleType
) -> Optional[List[str]]:
    """
    The generated statements migrate would actually apply, or None when
    the config module names no models.

    This is the drift preview without the drops: migrate generates none
    unless it is called from Python with allow_drops=True. The guards read
    this set, so a verdict names a statement the run would run. Each
    statement carries the generated migration's id, so a rule that reads
    migration boundaries sees these as one migration of their own.
    """
    models = getattr(config, "models", None)
    if not models:
        return None
    migration = migrator.plan(list(models))
    if migration is None:
        return []
    return [
        MigrationStatement(sql, migration.id, migration.transactional)
        for sql in migration_sql(migration, "up", migrator.compiler)
    ]


def _rehearsal_row_covers(migrator: Migrator, config: ModuleType) -> bool:
    """
    Whether a passing rehearsal already covers the run migrate would make,
    so the plan can point at migrate instead of rehearse.

    Two keys are tried: the pending migrations on their own, which is what
    a run without models applies, and the pending migrations plus the
    migration the models generate right now. The second is a guess. The
    real run diffs the models after the pending migrations have applied,
    and a pending migration that changes the same tables moves the
    generated statements, and with them the key. A wrong guess costs a
    stale suggestion; migrate still reads the row itself.
    """
    pending = migrator.pending()
    if not pending:
        return False
    records = migrator.read_applied_records()
    if migrator.rehearsed(rehearsal_key(records, pending)):
        return True
    models = getattr(config, "models", None)
    if not models:
        return False
    generated = migrator.plan(list(models))
    if generated is None:
        return False
    return migrator.rehearsed(rehearsal_key(records, pending + [generated]))


def _print_pending(summaries: List[PendingSummary]) -> None:
    print("pending")
    width = max(len(s.id) for s in summaries)
    for summary in summaries:
        if summary.sql is None:
            size = "callable step"
        else:
            size = _count(len(summary.sql), "statement")
        marker = ""
        if summary.repeatable:
            marker = "  repeat" + (" changed" if summary.state == "changed" else "")
        print(f"  {summary.id:<{width}}  {size}{marker}")
        for statement in summary.destructive:
            print(f"    destructive  {statement}")


def _plan_verdicts(
    config: ModuleType,
    summaries: List[PendingSummary],
    drift: Optional[List[str]],
    dialect: Dialects,
) -> Dict[str, List[Verdict]]:
    """
    The guards' verdicts on the statements migrate would apply, keyed by
    the normalized form of the statement they flag. A guard may report the
    statement in any form, so the key is normalized here and the readers
    normalize the statement they look up.

    The guards read the whole run at once, pending migrations and
    generated statements together, so a rule over the run as a whole sees
    what migrate will see. Each statement carries the id of the migration
    it belongs to, so a rule that reads migration boundaries reads the
    same ones migrate would. The drift the plan prints is the wider set: it
    includes the drops migrate does not generate, and no verdict is
    reported on those, because no run would read them.
    """
    guards = list(getattr(config, "guards", None) or [])
    statements = [s for summary in summaries for s in summary.sql or []]
    statements.extend(drift or [])
    by_statement: Dict[str, List[Verdict]] = {}
    for verdict in run_guards(guards, statements, dialect):
        by_statement.setdefault(normalize_statement(verdict.statement), []).append(
            verdict
        )
    return by_statement


def _statement_json(
    statements: Optional[List[str]],
    verdicts: Dict[str, List[Verdict]],
) -> Optional[List[Dict[str, JsonValue]]]:
    """
    One JSON object per statement, the same shape everywhere a command
    reports SQL: the statement, whether it removes data, and the guard
    verdicts on it. None stays None, for a callable step that renders no
    SQL. A verdict is reported on the statement it flags and nowhere
    else.
    """
    if statements is None:
        return None
    return [
        {
            "sql": statement,
            "destructive": bool(destructive_statements([statement])),
            "guards": [
                {"rule": v.rule, "verdict": v.verdict}
                for v in verdicts.get(normalize_statement(statement), [])
            ],
        }
        for statement in statements
    ]


def _plan_json(
    summaries: List[PendingSummary],
    problems: List[str],
    drift: Optional[List[str]],
    verdicts: Dict[str, List[Verdict]],
) -> None:
    """
    Prints the plan as one JSON object. `drift` is null, not an empty
    list, when the config module names no models: nothing was compared,
    which differs from comparing and finding no gap.
    """
    _print_json(
        {
            "pending": [
                {
                    "id": summary.id,
                    "state": summary.state,
                    "repeatable": summary.repeatable,
                    "statements": _statement_json(summary.sql, verdicts),
                    "destructive": summary.destructive,
                }
                for summary in summaries
            ],
            "problems": problems,
            "drift": _statement_json(drift, verdicts),
        }
    )


def _print_guards(verdicts: List[Verdict]) -> None:
    """
    The guards section: one line per verdict, the rule that objected and
    the statement it read.
    """
    print("guards")
    width = max(len(v.rule) for v in verdicts)
    for verdict in verdicts:
        print(f"  {verdict.verdict:<5}  {verdict.rule:<{width}}  {verdict.statement}")


def _cmd_plan(migrator: Migrator, args: argparse.Namespace, config: ModuleType) -> int:
    states = dict(migrator.statuses())
    summaries = [
        summarize(m, states.get(m.id, "pending"), migrator.compiler)
        for m in migrator.pending()
    ]
    problems = migrator.validate(raise_on_problems=False)
    drift = _drift_statements(migrator, config)
    by_statement = _plan_verdicts(
        config,
        summaries,
        _migrate_drift_statements(migrator, config),
        migrator.dialect,
    )
    verdicts = [v for group in by_statement.values() for v in group]
    blockers = blocking(verdicts)

    # Problems mean the plan itself cannot be trusted, so they outrank a
    # blocked statement, which outranks work merely waiting.
    if problems:
        exit_code = 1
    elif blockers:
        exit_code = 3
    elif summaries or drift:
        exit_code = 2
    else:
        exit_code = 0

    if args.json:
        _plan_json(summaries, problems, drift, by_statement)
        return exit_code

    sections: List[str] = []
    if summaries:
        _print_pending(summaries)
        sections.append(_count(len(summaries), "pending migration"))
    if problems:
        if sections:
            print()
        print("problems")
        for problem in problems:
            print(f"  {problem}")
        sections.append(_count(len(problems), "problem"))
    if drift:
        if sections:
            print()
        print("drift")
        for statement in drift:
            print(f"  {statement}")
        sections.append(_count(len(drift), "drift statement"))
    if verdicts:
        if sections:
            print()
        _print_guards(verdicts)
        sections.append(_count(len(verdicts), "guard verdict"))

    if not sections:
        if drift is None:
            print(
                "Nothing pending, no problems. Drift unchecked: the config "
                "module names no models."
            )
        else:
            print("Nothing pending, no problems, no drift.")
        return exit_code
    print()
    print(", ".join(sections))
    if blockers and not problems:
        # migrate refuses a blocked statement, so there is one thing to do
        # and it is not running migrate.
        print("blocked: fix the statement, or take the rule out of guards")
    elif not problems:
        # migrate never generates drops, so a drift section holding
        # nothing else is not work it can do.
        closable = [s for s in drift or [] if not destructive_statements([s])]
        if any(s.destructive for s in summaries) and not _rehearsal_row_covers(
            migrator, config
        ):
            # migrate refuses these until a rehearsal has proved them.
            print("run: sustained rehearse")
        elif summaries or closable:
            print("run: sustained migrate")
        elif drift:
            print(
                "migrate does not generate drops: write the migration by "
                "hand, or call Migrator.up(models, allow_drops=True)."
            )
    return exit_code


def _rehearsal_line(result: RehearsalResult, width: int) -> str:
    """
    One migration's line in the rehearsal report. The words after the id
    are what the rehearsal proved, in the order it proved them: the up
    step ran, the models landed, the down step ran, the schema came back.
    """
    if not result.up_ok:
        return f"failed    {result.id:<{width}}  up: {result.error}"
    proofs = ["up ok"]
    if result.landed is not None:
        proofs.append("landed" if not result.landed else "not landed")
    if result.down_ok:
        proofs.append("down ok")
    elif result.down_ok is False:
        proofs.append(f"down failed: {result.error}")
    else:
        proofs.append(str(result.error))
    if result.reversed is not None:
        proofs.append("reversed" if not result.reversed else "not reversed")
    return f"rehearsed {result.id:<{width}}  {', '.join(proofs)}"


def _report_rehearsal(results: Rehearsal, scratch: bool, note: Optional[str]) -> int:
    """
    Prints the rehearsal and returns the exit code: 1 when any step
    failed, when the models did not land, or when the schema did not come
    back, 0 otherwise. A migration whose down step could not be proved is
    not a failure; the line says so and the run still passes.
    """
    if not results:
        print("Nothing to rehearse.")
        return 0
    width = max(len(r.id) for r in results)
    for result in results:
        print(_rehearsal_line(result, width))
        for gap in result.landed or []:
            print(f"    outstanding  {gap}")
        for leftover in result.reversed or []:
            print(f"    leftover     {leftover}")
    if scratch:
        print("rehearsal complete on the scratch database")
    else:
        print("rollback complete, database unchanged")
    if not results.ok:
        print("run: sustained plan")
        return 1
    if note is not None:
        print(note)
    return 0


def _rehearsal_json(
    results: Rehearsal, scratch: bool, recorded: bool, key: str
) -> None:
    """
    Prints the rehearsal as one JSON object. `landed` and `reversed` are
    null when the check did not run, an empty list when it passed, and
    the lines naming the trouble when it failed. `key` names the content
    the run covered, and `recorded` says whether the row reached the
    database migrate will read.
    """
    _print_json(
        {
            "rehearsed": [
                {
                    "id": result.id,
                    "up_ok": result.up_ok,
                    "down_ok": result.down_ok,
                    "error": result.error,
                    "landed": result.landed,
                    "reversed": result.reversed,
                }
                for result in results
            ],
            "scratch": scratch,
            "key": key,
            "recorded": recorded,
            "ok": results.ok,
        }
    )


def _record_scratch_rehearsal_row(
    migrator: Migrator, results: Rehearsal
) -> Tuple[Optional[str], Optional[str]]:
    """
    Writes the row a passing scratch rehearsal earned onto the real
    database, where migrate will look for it. Returns the key and the
    line to print about it, either of which may be None.

    The scratch run starts from its own schema, so the key is computed
    against the real database's applied history and pending set. Nothing
    is written when the scratch run did not run every pending migration,
    since the row would then cover statements nothing proved.

    A row also goes in for every destructive prefix of the versioned
    pending list, the keys a `migrate --target` reads. The scratch run
    applied each of those prefixes on its way up and took them back on
    the way down, so it proved them too, and a real rehearsal records
    them the same way.
    """
    pending = migrator.pending()
    if not pending:
        return None, None
    proved = {r.id for r in results if r.up_ok}
    if any(m.id not in proved for m in pending):
        return None, (
            "rehearsal row not recorded: the scratch run did not cover every "
            "pending migration"
        )
    records = migrator.applied_records()
    key = rehearsal_key(records, pending)
    migrator.record_rehearsal(key)
    for prefix_key in _destructive_prefix_keys(records, pending, migrator.compiler):
        migrator.record_rehearsal(prefix_key)
    return key, "rehearsal row recorded"


def _cmd_rehearse(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    models = list(getattr(config, "models", None) or []) or None
    factory = getattr(config, "get_rehearsal_connection", None)
    scratch = factory is not None
    note: Optional[str] = None
    if factory is None:
        results = migrator.rehearse(models=models)
        key, recorded = results.key, results.recorded
        if recorded and results.ok:
            note = "rehearsal row recorded"
    else:
        connection = factory()
        try:
            results = _migrator_on(connection, config).rehearse(
                scratch=True, models=models
            )
        finally:
            _close_quietly(connection)
        key, recorded = results.key, False
        if results.ok:
            target_key, note = _record_scratch_rehearsal_row(migrator, results)
            if target_key is not None:
                key, recorded = target_key, True
    if args.json:
        _rehearsal_json(results, scratch, recorded, key)
        return 0 if results.ok else 1
    return _report_rehearsal(results, scratch, note)


def _cmd_migrate(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    models = list(getattr(config, "models", None) or []) or None
    if args.target is not None:
        # A generated migration always runs last, so a targeted run
        # applies the registered migrations only.
        models = None
    applied = migrator.up(
        target=args.target,
        validate=not args.no_validate,
        allow_out_of_order=args.allow_out_of_order,
        models=models,
        unrehearsed=args.unrehearsed,
    )
    if not applied:
        print("Nothing to apply.")
    for migration_id in applied:
        print(f"applied  {migration_id}")
    if models is not None:
        # Report only: the run has already happened, and a gap here is
        # something for the operator to look at, not a failure to raise.
        gaps = migrator.drift(models)
        for gap in gaps:
            print(f"drift    {gap}")
        if not gaps:
            print("schema matches the models")
    return 0


def _cmd_down(migrator: Migrator, args: argparse.Namespace, config: ModuleType) -> int:
    if args.to is not None:
        reverted = migrator.down_to(args.to, allow_changed=args.allow_changed)
    else:
        reverted = migrator.down(steps=args.steps, allow_changed=args.allow_changed)
    if not reverted:
        print("Nothing to revert.")
    for migration_id in reverted:
        print(f"reverted {migration_id}")
    return 0


def _cmd_validate(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    problems = migrator.validate(raise_on_problems=False)
    if args.json:
        _print_json({"ok": not problems, "problems": problems})
        return 1 if problems else 0
    if not problems:
        print("OK")
        return 0
    for problem in problems:
        print(f"problem  {problem}")
    return 1


def _cmd_repair(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    actions = migrator.repair()
    if not actions:
        print("Nothing to repair.")
    for action in actions:
        print(f"repaired {action}")
    return 0


def _cmd_script(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    print(migrator.script(args.direction))
    return 0


def _cmd_baseline(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    recorded = migrator.baseline(args.target)
    if not recorded:
        print("Nothing to baseline.")
    for migration_id in recorded:
        print(f"baselined {migration_id}")
    return 0


def _step_count(value: str) -> int:
    """
    Reads a --steps value and refuses anything below 1. A count of 0 or
    less asks the migrator to revert a number of migrations it cannot
    revert, so the command stops at the command line instead.
    """
    try:
        steps = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number.")
    if steps < 1:
        raise argparse.ArgumentTypeError("--steps must be 1 or more.")
    return steps


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sustained", description="Run sustained schema migrations."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def command(
        name: str, help_text: str, machine_readable: bool = False
    ) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--config",
            default="sustained_config",
            help="Config module to import (default: sustained_config).",
        )
        if machine_readable:
            sub.add_argument(
                "--json",
                action="store_true",
                help="Print one JSON object instead of the plain lines.",
            )
        return sub

    command(
        "status",
        "Show every migration's state: applied, pending, or changed.",
        machine_readable=True,
    )
    command(
        "plan",
        "Show the pending migrations, the problems, and the model drift.",
        machine_readable=True,
    )

    # Ordered as they are used: plan reads, rehearse proves, migrate applies.
    command(
        "rehearse",
        "Run the pending migrations up and back down, then roll it all back.",
        machine_readable=True,
    )

    migrate = command("migrate", "Apply pending migrations in order.")
    migrate.add_argument("--target", help="Stop after this migration id.")
    migrate.add_argument(
        "--no-validate", action="store_true", help="Skip validation before the run."
    )
    migrate.add_argument(
        "--allow-out-of-order",
        action="store_true",
        help="Accept a pending migration ordered before an applied one.",
    )
    migrate.add_argument(
        "--unrehearsed",
        action="store_true",
        help="Apply statements that remove data without a passing rehearsal.",
    )

    down = command("down", "Revert applied migrations, newest first.")
    group = down.add_mutually_exclusive_group()
    group.add_argument(
        "--steps",
        type=_step_count,
        default=1,
        help="How many migrations to revert (1 or more).",
    )
    group.add_argument("--to", help="Revert until this id is the newest applied.")
    down.add_argument(
        "--allow-changed",
        action="store_true",
        help="Revert a migration that was edited after it was applied.",
    )

    command(
        "validate",
        "Check the tracking table against the migrations.",
        machine_readable=True,
    )
    command("repair", "Fix tracking rows after failures or intentional edits.")

    script = command("script", "Print the SQL a run would execute.")
    script.add_argument("direction", nargs="?", choices=("up", "down"), default="up")

    baseline = command("baseline", "Record migrations as applied without running them.")
    baseline.add_argument("target", help="Record up to and including this id.")

    return parser


_COMMANDS = {
    "status": _cmd_status,
    "plan": _cmd_plan,
    "migrate": _cmd_migrate,
    "down": _cmd_down,
    "rehearse": _cmd_rehearse,
    "validate": _cmd_validate,
    "repair": _cmd_repair,
    "script": _cmd_script,
    "baseline": _cmd_baseline,
}


def _print_applied(error: BaseException) -> None:
    """
    Names the migrations that were already applied when a run stopped.

    A run with models reads the guards and the rehearsal row twice: once before
    anything runs, and once more against the migration generated from the
    models, whose statements exist only after the registered migrations
    have applied. A stop at that second reading leaves work behind, and
    the operator needs to know what.
    """
    for migration_id in getattr(error, "applied", None) or []:
        print(f"applied  {migration_id}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = _load_config(args.config)
        migrator, connection = _build_migrator(config)
    except Exception as error:
        # A config module that will not import, a connection that will not
        # open, a migrations directory that will not load: none of them
        # leave a connection behind, and none should reach the shell as a
        # traceback.
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        return _COMMANDS[args.command](migrator, args, config)
    except GuardBlocked as error:
        # Exit 3 says a guard blocked the run, which plan reports the same
        # way.
        _print_applied(error)
        print(f"error: {error}", file=sys.stderr)
        return 3
    except RehearsalRequired as error:
        # Exit 4 says the run needs a rehearsal it does not have, which is
        # a different thing to do from fixing a failure.
        _print_applied(error)
        print(f"error: {error}", file=sys.stderr)
        return 4
    except MigrationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # A driver raises its own error class, so a failing statement would
        # otherwise reach the shell as a traceback.
        migration_id = getattr(error, "migration_id", None)
        where = f" in '{migration_id}'" if migration_id else ""
        print(f"error{where}: {error}", file=sys.stderr)
        return 1
    finally:
        _close_quietly(connection)


if __name__ == "__main__":
    raise SystemExit(main())
