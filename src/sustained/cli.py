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
- `dialect`: a Dialects member or its name, such as 'postgres' (optional)
- `table`: the tracking table name (optional)
- `tracking_table_options`: TableOptions for the tracking table (optional)

Commands: status, migrate, down, validate, repair, script, baseline.
Every command exits 0 on success and 1 on failure.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from typing import Any, List, Optional, Sequence, Tuple

from sustained.dialects import Dialects
from sustained.exceptions import MigrationError
from sustained.migration_files import load_migrations
from sustained.migrations import Migration, Migrator


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
        migrations: List[Migration] = list(getattr(config, "migrations", []))
        directory = getattr(config, "migrations_dir", None)
        if directory is not None:
            migrations.extend(
                load_migrations(
                    directory, placeholders=getattr(config, "placeholders", None)
                )
            )
        migrator = Migrator(
            connection,
            migrations,
            table=getattr(config, "table", "sustained_migrations"),
            dialect=_resolve_dialect(getattr(config, "dialect", None)),
            tracking_table_options=getattr(config, "tracking_table_options", None),
        )
    except Exception:
        # The connection never reaches the caller on a setup failure, so it
        # must close here.
        _close_quietly(connection)
        raise
    return migrator, connection


def _cmd_status(migrator: Migrator, args: argparse.Namespace) -> int:
    for migration_id, state in migrator.statuses():
        print(f"{state:8} {migration_id}")
    return 0


def _cmd_migrate(migrator: Migrator, args: argparse.Namespace) -> int:
    applied = migrator.up(
        target=args.target,
        validate=not args.no_validate,
        allow_out_of_order=args.allow_out_of_order,
    )
    if not applied:
        print("Nothing to apply.")
    for migration_id in applied:
        print(f"applied  {migration_id}")
    return 0


def _cmd_down(migrator: Migrator, args: argparse.Namespace) -> int:
    if args.to is not None:
        reverted = migrator.down_to(args.to)
    else:
        reverted = migrator.down(steps=args.steps)
    if not reverted:
        print("Nothing to revert.")
    for migration_id in reverted:
        print(f"reverted {migration_id}")
    return 0


def _cmd_validate(migrator: Migrator, args: argparse.Namespace) -> int:
    problems = migrator.validate(raise_on_problems=False)
    if not problems:
        print("OK")
        return 0
    for problem in problems:
        print(f"problem  {problem}")
    return 1


def _cmd_repair(migrator: Migrator, args: argparse.Namespace) -> int:
    actions = migrator.repair()
    if not actions:
        print("Nothing to repair.")
    for action in actions:
        print(f"repaired {action}")
    return 0


def _cmd_script(migrator: Migrator, args: argparse.Namespace) -> int:
    print(migrator.script(args.direction))
    return 0


def _cmd_baseline(migrator: Migrator, args: argparse.Namespace) -> int:
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

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--config",
            default="sustained_config",
            help="Config module to import (default: sustained_config).",
        )
        return sub

    command("status", "Show every migration's state: applied, pending, or changed.")

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

    command("validate", "Check the tracking table against the migrations.")
    command("repair", "Fix tracking rows after failures or intentional edits.")

    script = command("script", "Print the SQL a run would execute.")
    script.add_argument("direction", nargs="?", choices=("up", "down"), default="up")

    baseline = command("baseline", "Record migrations as applied without running them.")
    baseline.add_argument("target", help="Record up to and including this id.")

    return parser


_COMMANDS = {
    "status": _cmd_status,
    "migrate": _cmd_migrate,
    "down": _cmd_down,
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
        return _COMMANDS[args.command](migrator, args)
    except MigrationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        _close_quietly(connection)


if __name__ == "__main__":
    raise SystemExit(main())
