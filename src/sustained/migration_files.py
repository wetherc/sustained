"""
Migrations loaded from SQL files.

A migrations directory holds one '<id>.up.sql' file per migration and an
optional '<id>.down.sql' with the reverse steps. Ids order the run
lexicographically, so number them: '0001_create_users.up.sql',
'0002_add_flag.up.sql'. load_migrations() turns the directory into the
ordered Migration list a Migrator takes.

A '<id>.repeat.sql' file is a repeatable migration: it re-runs whenever
its contents change, for views, functions, and seed data. Repeatables
have no down file, sort after every versioned migration, and an id may
not have both an up file and a repeat file.

Files split into statements at semicolons that end a line, with or
without a '--' comment after the semicolon. A body with
embedded semicolons, such as a trigger or procedure, does not survive
that; write it as a hand-written Migration with a callable step, or keep
it as the only statement in its file with no trailing semicolon.

A file whose first lines hold the marker comment `-- sustained: no
transaction` runs outside a transaction. Write it as `-- sustained: no
transaction` on a line of its own, anywhere in the up file or the repeat
file. The marker sets `transactional=False` on the Migration, so the up
step and the down step both run bare. Use it for a statement the engine
refuses inside a transaction block, such as CREATE INDEX CONCURRENTLY on
Postgres. A migration like that which fails part way leaves the
statements before the failure applied.

Files may hold `${key}` placeholders, filled from the mapping passed to
load_migrations(). Substitution happens before checksums are computed,
so a changed value reads as a changed migration. `$${` escapes to a
literal `${`. A malformed marker, such as `${my-key}` or an unclosed
`${key`, raises an error. With no mapping, files load untouched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Union

from sustained.migrations import Migration

# A statement ends at a semicolon that ends its line. A trailing '--'
# comment after the semicolon is part of that line, so it does not glue
# the next statement onto this one.
_STATEMENT_END_RE = re.compile(r";[ \t]*(?:--[^\n]*)?\n")

_UP_SUFFIX = ".up.sql"
_DOWN_SUFFIX = ".down.sql"
_REPEAT_SUFFIX = ".repeat.sql"

# Names the naming check passes over: the files an editor or a tool
# leaves next to the migrations. A dotfile is hidden and never a
# migration, and these suffixes are backup or swap copies.
_IGNORED_SUFFIXES = ("~", ".bak", ".orig", ".swp", ".swo", ".tmp")

_PLACEHOLDER_RE = re.compile(r"\$\$\{|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$\{")

# The marker that takes a file migration out of its transaction. It is a
# comment line of its own, so the file is still valid SQL and the marker
# reaches the database as a comment. Spelling is loose about case and
# about the spaces around the words, and nothing else may share the line.
_NO_TRANSACTION_RE = re.compile(
    r"^[ \t]*--[ \t]*sustained:[ \t]*no[ \t_-]transaction[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def declares_no_transaction(text: str) -> bool:
    """
    Whether a migration file asks to run outside a transaction with the
    '-- sustained: no transaction' marker on a line of its own. The
    marker may sit anywhere in the file. Case does not matter, and the
    word pair may be joined by a space, a hyphen, or an underscore.
    """
    return _NO_TRANSACTION_RE.search(text) is not None


def substitute_placeholders(
    text: str, placeholders: Optional[Dict[str, str]], source: str
) -> str:
    """
    Replaces every '${key}' in the text with its value from the mapping
    and turns the '$${' escape into a literal '${'. A '${key}' with no
    value raises ValueError naming the source file and the key. A '${'
    that does not open a well-formed marker, such as '${my-key}' or an
    unclosed '${key', also raises ValueError. Both rules keep a typo
    from reaching the database as raw text. When the mapping is None,
    substitution is off and the text returns untouched.
    """
    if placeholders is None:
        return text
    values = placeholders

    def _replace(match: "re.Match[str]") -> str:
        if match.group(0) == "$${":
            return "${"
        key = match.group(1)
        if key is None:
            snippet = text[match.start() : match.start() + 20]
            raise ValueError(
                f"Migration file {source!r} has a malformed placeholder "
                f"near {snippet!r}. Placeholder keys must look like "
                "'${valid_identifier}'; '$${' escapes a literal '${'."
            )
        if key not in values:
            raise ValueError(
                f"Migration file {source!r} uses placeholder '${{{key}}}' "
                "with no value; add it to placeholders."
            )
        return str(values[key])

    return _PLACEHOLDER_RE.sub(_replace, text)


def _ignored_name(name: str) -> bool:
    """
    Whether the naming check passes over a file. A dotfile is hidden, and
    an editor backup or swap file is a copy of another file, so neither
    one is a migration somebody misnamed.
    """
    return name.startswith(".") or name.endswith(_IGNORED_SUFFIXES)


def split_sql_statements(text: str) -> List[str]:
    """
    Splits a SQL file's contents into statements at semicolons that end a
    line, with or without a '--' comment after the semicolon. Pieces
    holding only whitespace or '--' comments are dropped; a missing final
    semicolon is fine.
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


def load_migrations(
    directory: Union[str, Path],
    placeholders: Optional[Dict[str, str]] = None,
) -> List[Migration]:
    """
    Reads every '<id>.up.sql' file in the directory, pairs it with its
    '<id>.down.sql' when one exists, and returns the migrations ordered
    by id, followed by the '<id>.repeat.sql' repeatables in id order.
    Raises when the directory is missing, a file is empty, a down file
    has no up file, an id has both an up file and a repeat file, or a
    file follows no naming pattern, so a misnamed migration cannot be
    skipped silently. Every file in the directory is checked, whatever
    its extension, because a typo like '0002_add.up.sq' is the case the
    check is for. Directories are passed over, and so are dotfiles and
    editor backup files ('*~', '*.bak', '*.orig', '*.swp', '*.swo',
    '*.tmp'), which are copies of another file rather than migrations.

    A '-- sustained: no transaction' line in an up file or a repeat file
    gives the migration transactional=False, so the migrator runs it
    outside a transaction. The marker is read from the up file and the
    repeat file only. A down file that holds it changes nothing, because
    the flag belongs to the migration and already covers the down step.

    When a placeholders mapping is given, even an empty one, '${key}'
    markers in the files are filled from it before statements split and
    checksums compute, and an unknown key raises ValueError. When it is
    None, the files load untouched.
    """
    path = Path(directory)
    if not path.is_dir():
        raise ValueError(f"Migrations directory not found: {path}")

    ups = {}
    downs = {}
    repeats = {}
    for entry in sorted(path.iterdir()):
        if entry.is_dir() or _ignored_name(entry.name):
            continue
        if entry.name.endswith(_UP_SUFFIX):
            ups[entry.name[: -len(_UP_SUFFIX)]] = entry
        elif entry.name.endswith(_DOWN_SUFFIX):
            downs[entry.name[: -len(_DOWN_SUFFIX)]] = entry
        elif entry.name.endswith(_REPEAT_SUFFIX):
            repeats[entry.name[: -len(_REPEAT_SUFFIX)]] = entry
        else:
            raise ValueError(
                f"Migration file {entry.name!r} matches none of "
                f"'*{_UP_SUFFIX}', '*{_DOWN_SUFFIX}', '*{_REPEAT_SUFFIX}'; "
                "rename it so it cannot be skipped silently, or move it "
                "out of the migrations directory."
            )

    both = sorted(set(ups) & set(repeats))
    if both:
        raise ValueError(
            f"Ids with both an up file and a repeat file: {', '.join(both)}. "
            "A migration is versioned or repeatable, not both."
        )
    orphaned = sorted(set(downs) - set(ups))
    if orphaned:
        raise ValueError(f"Down files without an up file: {', '.join(orphaned)}.")

    def read(entry: Path) -> str:
        text = entry.read_text(encoding="utf-8")
        # None means substitution is off, so files that happen to contain
        # '${...}' keep loading as they did before placeholders existed.
        if placeholders is None:
            return text
        return substitute_placeholders(text, placeholders, entry.name)

    migrations: List[Migration] = []
    for id in sorted(ups):
        up_text = read(ups[id])
        up_statements = split_sql_statements(up_text)
        if not up_statements:
            raise ValueError(f"Migration file {ups[id].name!r} has no statements.")
        down_statements = None
        if id in downs:
            down_statements = split_sql_statements(read(downs[id]))
            if not down_statements:
                raise ValueError(
                    f"Migration file {downs[id].name!r} has no statements; "
                    "delete it if the migration is not reversible."
                )
        migrations.append(
            Migration(
                id,
                up=up_statements,
                down=down_statements,
                transactional=not declares_no_transaction(up_text),
            )
        )
    for id in sorted(repeats):
        repeat_text = read(repeats[id])
        statements = split_sql_statements(repeat_text)
        if not statements:
            raise ValueError(f"Migration file {repeats[id].name!r} has no statements.")
        migrations.append(
            Migration(
                id,
                up=statements,
                repeatable=True,
                transactional=not declares_no_transaction(repeat_text),
            )
        )
    return migrations
