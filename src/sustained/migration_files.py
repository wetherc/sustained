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
without a '--' comment after the semicolon. A semicolon inside a string
literal, a quoted identifier, a /* */ comment, or a Postgres
dollar-quoted body never splits, so a function written as
`AS $$ ... $$` stays one statement. For a MySQL trigger or procedure,
whose body has bare semicolons, put a `DELIMITER //` line before it and
`DELIMITER ;` after it, as the mysql client reads them: '//' then ends
each statement, and the directive lines are dropped.

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
from bisect import bisect_right
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from sustained.migrations import Migration

# A statement ends at a semicolon that ends its line. A trailing '--'
# comment after the semicolon is part of that line, so it does not glue
# the next statement onto this one.
_STATEMENT_END_RE = re.compile(r";[ \t]*(?:--[^\n]*)?\n")

# A Postgres dollar quote opens with $$ or $tag$ and closes with the same
# text.
_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")

# A mysql client DELIMITER line, which sets the text that ends the
# statements after it. A token with a quote in it is not a directive, so a
# Postgres COPY option such as DELIMITER ',' on a line of its own stays SQL.
_DELIMITER_RE = re.compile(
    r"^[ \t]*DELIMITER[ \t]+([^\s'\"]+)[ \t]*$", re.IGNORECASE | re.MULTILINE
)

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


def _quote_end(text: str, start: int, backslash: bool) -> Optional[int]:
    """
    The index just past the quote that closes the one at `start`, or None
    when the text ends first. A doubled quote stands for itself. With
    `backslash`, a backslash also escapes the next character, as MySQL
    and Postgres E'' strings read it. Backticks take no backslash escape.
    """
    quote = text[start]
    index = start + 1
    while index < len(text):
        char = text[index]
        if backslash and char == "\\" and quote != "`":
            index += 2
        elif char != quote:
            index += 1
        elif text.startswith(quote, index + 1):
            index += 2
        else:
            return index + 1
    return None


def _quoted_spans(
    text: str, backslash: bool, dollar: bool = True
) -> Optional[List[Tuple[int, int]]]:
    """
    The (start, end) index pairs of every string literal, quoted
    identifier, /* */ comment, and dollar-quoted body in the text, or
    None when one of them is still open at the end. `backslash` says
    whether a backslash escapes the next character inside quotes, and
    `dollar` whether a dollar quote opens a span. MySQL has no dollar
    quotes, and its files often set $$ as the delimiter.

    A '--' comment is skipped so a quote inside it opens nothing, but it
    is not a span, and a line-ending semicolon in one splits the file. A
    commented-out statement on a line of its own is then a comment-only
    piece that drops out. Glued onto the next statement instead, it would
    change that statement's text and so the checksum of a file already
    applied.
    """
    spans: List[Tuple[int, int]] = []
    index = 0
    while index < len(text):
        char = text[index]
        end: Optional[int] = None
        if text.startswith("--", index):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
            continue
        if text.startswith("/*", index):
            close = text.find("*/", index + 2)
            end = None if close < 0 else close + 2
        elif char in "'\"`":
            end = _quote_end(text, index, backslash)
        elif (
            dollar and char == "$" and not (index and _identifier_char(text[index - 1]))
        ):
            tag = _DOLLAR_TAG_RE.match(text, index)
            if tag is None:
                index += 1
                continue
            close = text.find(tag.group(0), tag.end())
            end = None if close < 0 else close + len(tag.group(0))
        else:
            index += 1
            continue
        if end is None:
            return None
        spans.append((index, end))
        index = end
    return spans


def _identifier_char(char: str) -> bool:
    """Whether the character can sit inside an unquoted identifier."""
    return char.isalnum() or char in "_$"


def _outside(position: int, starts: List[int], spans: List[Tuple[int, int]]) -> bool:
    """
    Whether the position falls outside every span in the sorted list.
    `starts` lists the spans' start indexes, in the same order.
    """
    at = bisect_right(starts, position) - 1
    return at < 0 or position >= spans[at][1]


def _statement_ends(text: str) -> List[Tuple[int, int]]:
    """
    The line-ending semicolons that end a statement: every match of the
    split rule outside the quoted spans.

    The file does not say whether a backslash escapes a quote, so the
    text is read both ways, and a match counts when either reading puts
    it outside every span. A MySQL 'it\\'s' read without escapes, or a
    standard 'C:\\' read with them, opens a string that swallows the next
    statement end, and the other reading still finds it. A reading that
    leaves a quote or comment open at the end is dropped. When both do,
    the file splits at every match, so a quote the scan misreads cannot
    merge a file into one statement.

    The one misread left needs a string that holds an escaped quote and
    a line-ending semicolon, in a file whose quotes also balance when a
    backslash escapes nothing. The reading without escapes then ends the
    string early and splits at that semicolon.
    """
    ends = [(m.start(), m.end()) for m in _STATEMENT_END_RE.finditer(text)]
    return _unquoted_ends(text, ends, dollar=True)


def _unquoted_ends(
    text: str, ends: List[Tuple[int, int]], dollar: bool
) -> List[Tuple[int, int]]:
    """
    The candidate statement ends, as (start, end) index pairs, that
    either reading of the quotes puts outside every span, or all of them
    when both readings leave a quote or comment open.
    """
    readings = [
        ([start for start, _ in spans], spans)
        for spans in (
            _quoted_spans(text, False, dollar),
            _quoted_spans(text, True, dollar),
        )
        if spans is not None
    ]
    if not readings:
        return ends
    return [
        end
        for end in ends
        if any(_outside(end[0], starts, spans) for starts, spans in readings)
    ]


def _delimited_ends(text: str, delimiter: str) -> List[Tuple[int, int]]:
    """
    Where a statement ends in text that a DELIMITER line set to something
    other than ';': every occurrence of the delimiter outside quotes and
    comments, wherever it sits on its line, as the mysql client reads it.
    """
    ends = []
    index = text.find(delimiter)
    while index >= 0:
        ends.append((index, index + len(delimiter)))
        index = text.find(delimiter, index + len(delimiter))
    return _unquoted_ends(text, ends, dollar=False)


def _delimiter_sections(text: str) -> List[Tuple[str, str]]:
    """
    The text cut at its DELIMITER lines, each part paired with the
    delimiter in force for it. The directive lines themselves are not
    SQL and are dropped.
    """
    sections = []
    delimiter = ";"
    previous = 0
    for directive in _DELIMITER_RE.finditer(text):
        sections.append((text[previous : directive.start()], delimiter))
        delimiter = directive.group(1)
        previous = directive.end()
    sections.append((text[previous:], delimiter))
    return sections


def split_sql_statements(text: str) -> List[str]:
    """
    Splits a SQL file's contents into statements at semicolons that end a
    line, with or without a '--' comment after the semicolon. A semicolon
    inside a string literal, a quoted identifier, a /* */ comment, or a
    dollar-quoted body does not split. Pieces holding only whitespace or
    '--' comments are dropped; a missing final semicolon is fine.

    A 'DELIMITER //' line, as the mysql client reads it, makes '//' end
    the statements after it, wherever it sits on a line, until a
    'DELIMITER ;' line. A trigger or procedure body keeps its own
    semicolons that way.
    """
    pieces = []
    for section, delimiter in _delimiter_sections(text):
        if delimiter == ";":
            ends = _statement_ends(section)
        else:
            ends = _delimited_ends(section, delimiter)
        previous = 0
        for start, end in ends:
            pieces.append(section[previous:start])
            previous = end
        pieces.append(section[previous:])
    statements = []
    for piece in pieces:
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
