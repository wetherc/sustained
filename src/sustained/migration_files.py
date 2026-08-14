"""
Migrations loaded from SQL files.

A migrations directory holds one '<id>.up.sql' file per migration and an
optional '<id>.down.sql' with the reverse steps. Ids order the run
lexicographically, so number them: '0001_create_users.up.sql',
'0002_add_flag.up.sql'. load_migrations() turns the directory into the
ordered Migration list a Migrator takes.

Files split into statements at semicolons that end a line. A body with
embedded semicolons, such as a trigger or procedure, does not survive
that; write it as a hand-written Migration with a callable step, or keep
it as the only statement in its file with no trailing semicolon.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Union

from sustained.migrations import Migration

_STATEMENT_END_RE = re.compile(r";[ \t]*\n")

_UP_SUFFIX = ".up.sql"
_DOWN_SUFFIX = ".down.sql"


def split_sql_statements(text: str) -> List[str]:
    """
    Splits a SQL file's contents into statements at semicolons that end a
    line. Pieces holding only whitespace or '--' comments are dropped; a
    missing final semicolon is fine.
    """
    statements = []
    for piece in _STATEMENT_END_RE.split(text):
        cleaned = piece.strip()
        if not cleaned:
            continue
        lines = [line.strip() for line in cleaned.splitlines()]
        if all(not line or line.startswith("--") for line in lines):
            continue
        statements.append(cleaned.rstrip(";").rstrip())
    return statements


def load_migrations(directory: Union[str, Path]) -> List[Migration]:
    """
    Reads every '<id>.up.sql' file in the directory, pairs it with its
    '<id>.down.sql' when one exists, and returns the migrations ordered by
    id. Raises when the directory is missing, an up file is empty, a down
    file has no up file, or a '.sql' file follows neither naming pattern,
    so a misnamed migration cannot be skipped silently.
    """
    path = Path(directory)
    if not path.is_dir():
        raise ValueError(f"Migrations directory not found: {path}")

    ups = {}
    downs = {}
    for entry in sorted(path.iterdir()):
        if not entry.is_file() or not entry.name.endswith(".sql"):
            continue
        if entry.name.endswith(_UP_SUFFIX):
            ups[entry.name[: -len(_UP_SUFFIX)]] = entry
        elif entry.name.endswith(_DOWN_SUFFIX):
            downs[entry.name[: -len(_DOWN_SUFFIX)]] = entry
        else:
            raise ValueError(
                f"Migration file {entry.name!r} matches neither "
                f"'*{_UP_SUFFIX}' nor '*{_DOWN_SUFFIX}'; rename it so it "
                "cannot be skipped silently."
            )

    orphaned = sorted(set(downs) - set(ups))
    if orphaned:
        raise ValueError(f"Down files without an up file: {', '.join(orphaned)}.")

    migrations: List[Migration] = []
    for id in sorted(ups):
        up_statements = split_sql_statements(ups[id].read_text(encoding="utf-8"))
        if not up_statements:
            raise ValueError(f"Migration file {ups[id].name!r} has no statements.")
        down_statements = None
        if id in downs:
            down_statements = split_sql_statements(
                downs[id].read_text(encoding="utf-8")
            )
            if not down_statements:
                raise ValueError(
                    f"Migration file {downs[id].name!r} has no statements; "
                    "delete it if the migration is not reversible."
                )
        migrations.append(Migration(id, up=up_statements, down=down_statements))
    return migrations
