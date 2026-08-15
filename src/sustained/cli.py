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
- `tracking_table_options`: TableOptions for the tracking table (optional)
- `get_rehearsal_connection()`: a connection to a scratch database, which
  `rehearse` then uses instead of the real one (optional)
- `before_migrate(connection)`, `after_migrate(connection, applied)`, and
  `on_error(connection, migration_id, error)`: callbacks around the
  `migrate` command (optional)

Commands: status, plan, migrate, rehearse, down, validate, repair,
script, baseline. Every command exits 0 on success and 1 on failure.
`plan` exits 2 when work is waiting.

`status`, `validate`, and `plan` take `--json`, which prints one JSON
object instead of the plain lines. The exit code stays the same either
way.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from typing import Any, List, Optional, Sequence, Tuple

from sustained.analysis import PendingSummary, summarize
from sustained.dialects import Dialects
from sustained.exceptions import MigrationError
from sustained.migration_files import load_migrations
from sustained.migrations import (
    Migration,
    Migrator,
    RehearsalResult,
    migration_sql,
)


def _resolve_dialect(value: Any) -> Dialects:
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


def _load_config(module_name: str) -> Any:
    sys.path.insert(0, os.getcwd())
    try:
        return importlib.import_module(module_name)
    finally:
        sys.path.pop(0)


def _close_quietly(connection: Any) -> None:
    if hasattr(connection, "close"):
        try:
            connection.close()
        except Exception:
            pass


def _migrator_on(connection: Any, config: Any) -> Migrator:
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
        dialect=_resolve_dialect(getattr(config, "dialect", None)),
        tracking_table_options=getattr(config, "tracking_table_options", None),
    )


def _build_migrator(config: Any) -> Tuple[Migrator, Any]:
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


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2))


def _cmd_status(migrator: Migrator, args: argparse.Namespace, config: Any) -> int:
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


def _drift_statements(migrator: Migrator, config: Any) -> Optional[List[str]]:
    """
    The statements that would close the gap between the config module's
    models and the database, or None when the module names no models.

    Drops are included: a preview reports every difference, including
    tables and columns the models no longer declare, which sync() would
    refuse to generate. The statements print in full, so a drop reads as
    a drop without a separate label.
    """
    models = getattr(config, "models", None)
    if not models:
        return None
    migration = migrator.plan(list(models), allow_drops=True)
    if migration is None:
        return []
    return migration_sql(migration, "up")


def _print_pending(summaries: List[PendingSummary]) -> None:
    print("pending")
    width = max(len(s.id) for s in summaries)
    for summary in summaries:
        if summary.statements is None:
            size = "callable step"
        else:
            size = _count(summary.statements, "statement")
        marker = ""
        if summary.repeatable:
            marker = "  repeat" + (" changed" if summary.state == "changed" else "")
        print(f"  {summary.id:<{width}}  {size}{marker}")
        for statement in summary.destructive:
            print(f"    destructive  {statement}")


def _plan_json(
    summaries: List[PendingSummary], problems: List[str], drift: Optional[List[str]]
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
                    "statements": summary.statements,
                    "destructive": summary.destructive,
                }
                for summary in summaries
            ],
            "problems": problems,
            "drift": drift,
        }
    )


def _cmd_plan(migrator: Migrator, args: argparse.Namespace, config: Any) -> int:
    states = dict(migrator.statuses())
    summaries = [summarize(m, states.get(m.id, "pending")) for m in migrator.pending()]
    problems = migrator.validate(raise_on_problems=False)
    drift = _drift_statements(migrator, config)

    if problems:
        exit_code = 1
    elif summaries or drift:
        exit_code = 2
    else:
        exit_code = 0

    if args.json:
        _plan_json(summaries, problems, drift)
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
    if not problems and summaries:
        print("run: sustained migrate")
    return exit_code


def _rehearsal_line(result: RehearsalResult, width: int) -> str:
    """One migration's line in the rehearsal report."""
    if not result.up_ok:
        return f"failed    {result.id:<{width}}  up: {result.error}"
    if result.down_ok:
        outcome = "down ok"
    elif result.down_ok is False:
        outcome = f"down failed: {result.error}"
    else:
        outcome = str(result.error)
    return f"rehearsed {result.id:<{width}}  up ok, {outcome}"


def _report_rehearsal(results: List[RehearsalResult], scratch: bool) -> int:
    """
    Prints the rehearsal and returns the exit code: 1 when any step
    failed, 0 otherwise. A migration whose down step could not be proved
    is not a failure; the line says so and the run still passes.
    """
    if not results:
        print("Nothing to rehearse.")
        return 0
    width = max(len(r.id) for r in results)
    for result in results:
        print(_rehearsal_line(result, width))
    if scratch:
        print("rehearsal complete on the scratch database")
    else:
        print("rollback complete, database unchanged")
    return 1 if any(not r.up_ok or r.down_ok is False for r in results) else 0


def _cmd_rehearse(migrator: Migrator, args: argparse.Namespace, config: Any) -> int:
    factory = getattr(config, "get_rehearsal_connection", None)
    if factory is None:
        return _report_rehearsal(migrator.rehearse(), scratch=False)
    scratch = factory()
    try:
        results = _migrator_on(scratch, config).rehearse(scratch=True)
    finally:
        _close_quietly(scratch)
    return _report_rehearsal(results, scratch=True)


def _callback(config: Any, name: str) -> Optional[Any]:
    """The named callback from the config module, or None when it has none."""
    hook = getattr(config, name, None)
    return hook if callable(hook) else None


def _call_on_error(hook: Any, connection: Any, error: BaseException) -> None:
    """
    Hands a failed run to the config module's on_error callback. A callback
    that raises is reported on stderr and then set aside, so the migration
    error is the one that reaches the operator.
    """
    try:
        hook(connection, getattr(error, "migration_id", None), error)
    except Exception as callback_error:
        print(f"error: on_error raised {callback_error!r}", file=sys.stderr)


def _cmd_migrate(migrator: Migrator, args: argparse.Namespace, config: Any) -> int:
    before = _callback(config, "before_migrate")
    if before is not None:
        before(migrator.connection)
    try:
        applied = migrator.up(
            target=args.target,
            validate=not args.no_validate,
            allow_out_of_order=args.allow_out_of_order,
        )
    except Exception as error:
        hook = _callback(config, "on_error")
        if hook is not None:
            _call_on_error(hook, migrator.connection, error)
        raise
    if not applied:
        print("Nothing to apply.")
    for migration_id in applied:
        print(f"applied  {migration_id}")
    after = _callback(config, "after_migrate")
    if after is not None and applied:
        after(migrator.connection, applied)
    return 0


def _cmd_down(migrator: Migrator, args: argparse.Namespace, config: Any) -> int:
    if args.to is not None:
        reverted = migrator.down_to(args.to)
    else:
        reverted = migrator.down(steps=args.steps)
    if not reverted:
        print("Nothing to revert.")
    for migration_id in reverted:
        print(f"reverted {migration_id}")
    return 0


def _cmd_validate(migrator: Migrator, args: argparse.Namespace, config: Any) -> int:
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


def _cmd_repair(migrator: Migrator, args: argparse.Namespace, config: Any) -> int:
    actions = migrator.repair()
    if not actions:
        print("Nothing to repair.")
    for action in actions:
        print(f"repaired {action}")
    return 0


def _cmd_script(migrator: Migrator, args: argparse.Namespace, config: Any) -> int:
    print(migrator.script(args.direction))
    return 0


def _cmd_baseline(migrator: Migrator, args: argparse.Namespace, config: Any) -> int:
    recorded = migrator.baseline(args.target)
    if not recorded:
        print("Nothing to baseline.")
    for migration_id in recorded:
        print(f"baselined {migration_id}")
    return 0


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

    down = command("down", "Revert applied migrations, newest first.")
    group = down.add_mutually_exclusive_group()
    group.add_argument(
        "--steps", type=int, default=1, help="How many migrations to revert."
    )
    group.add_argument("--to", help="Revert until this id is the newest applied.")

    command(
        "validate",
        "Check the tracking table against the migrations.",
        machine_readable=True,
    )
    command(
        "rehearse",
        "Run the pending migrations up and back down, then roll it all back.",
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = _load_config(args.config)
        migrator, connection = _build_migrator(config)
    except (ImportError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        return _COMMANDS[args.command](migrator, args, config)
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
