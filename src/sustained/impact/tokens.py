"""
The lexical layer shared by the textual scan and the impact recognizer.

Two consumers read statement text here. `sustained.analysis` scans the
words of a statement for drops and keeps literals and comments out of
the scan; it takes the token patterns below. The impact recognizer reads
a statement as a token stream; it takes `tokenize()`.

Both read the same literal and comment rules, so the scan and the
recognizer cannot disagree about where a literal ends:

- a string literal is `'...'`, with `''` as an escaped quote
- a quoted identifier is `"..."`, with `""` as an escaped quote, or a
  MySQL `` `...` ``
- a comment is `-- ...` to the end of the line, or `/* ... */`
- a Postgres dollar-quoted string is `$$...$$` or `$tag$...$tag$`

A dollar tag follows the identifier rules Postgres gives it: letters,
digits, and underscores, not starting with a digit, and not glued to the
end of a word. So `$1` is a parameter and `a$b$c` is one word.

`tokenize()` also reads what only some dialects have: MySQL backslash
escapes inside literals, MySQL double-quoted strings, and `[...]`
identifiers on SQL Server and SQLite. The textual scan has no dialect,
so it reads the standard form and, when the statement holds a backslash,
the MySQL form as well.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, NamedTuple, Optional, Tuple

if TYPE_CHECKING:
    from sustained.dialects import Dialects

# A Postgres dollar quote: an opening tag, the body, and the same tag
# again. The tag may be empty, and its group then matches the empty
# string: a backreference to a group that took no part never matches, so
# `$$...$$` needs the group to take part. The lookbehind keeps a `$`
# inside a word such as `a$b$c` from opening a quote, as Postgres reads
# it.
_DOLLAR_QUOTED = r"(?<![\w$])\$(?P<tag>(?:[A-Za-z_][A-Za-z_0-9]*)?)\$.*?\$(?P=tag)\$"

# The alternatives the textual scan treats as tokens after a literal: a
# quoted identifier, a comment, or a dollar-quoted body. A comment inside
# a literal is part of the literal, so the literal alternatives come
# first and a '--' inside quotes survives.
NON_LITERAL_TOKENS = (
    r'|"(?:[^"]|"")*"'  # quoted identifier
    r"|`[^`]*`"  # MySQL quoted identifier
    r"|--[^\n]*"  # line comment
    r"|/\*.*?\*/"  # block comment
    r"|" + _DOLLAR_QUOTED  # Postgres dollar-quoted body
)
# '' is an escaped quote inside a string literal.
TOKEN_RE = re.compile(r"'(?:[^']|'')*'" + NON_LITERAL_TOKENS, re.DOTALL)
# MySQL also reads a backslash inside a literal as an escape. There
# 'it\'s' is one literal, and a scan with the standard reading ends the
# literal at the backslash, so a quote later in the statement opens a
# literal that hides a real DROP. A statement with a backslash is
# scanned with both readings.
BACKSLASH_TOKEN_RE = re.compile(r"'(?:[^'\\]|''|\\.)*'" + NON_LITERAL_TOKENS, re.DOTALL)

# Token kinds. A keyword is a WORD; the recognizer compares its upper
# case form. An unterminated literal, quoted identifier, dollar quote, or
# block comment yields one ERROR token for the rest of the statement, so
# nothing reads text whose quoting is in doubt.
WORD = "word"
IDENT = "ident"
STRING = "string"
NUMBER = "number"
PARAM = "param"
PUNCT = "punct"
OP = "op"
ERROR = "error"


class Token(NamedTuple):
    """
    One lexical token. `text` is the token as the statement spells it.
    `value` is what it means: the upper case form of a word, the name
    inside the quotes of a quoted identifier, the contents of a string
    literal with its escapes undone, and the text itself for the rest.
    `start` is the offset of the token in the statement.
    """

    kind: str
    text: str
    value: str
    start: int

    def is_word(self, *words: str) -> bool:
        """Whether this is a bare word, and when words are given, one of them."""
        return self.kind == WORD and (not words or self.value in words)

    @property
    def name(self) -> Optional[str]:
        """The identifier this token names, bare or quoted, or None."""
        if self.kind == IDENT:
            return self.value
        if self.kind == WORD:
            return self.text
        return None


class _Lexicon(NamedTuple):
    backslash_escapes: bool
    double_quoted_strings: bool
    bracket_identifiers: bool
    dollar_quotes: bool


_STANDARD = _Lexicon(False, False, True, False)


def _lexicon(dialect: Optional["Dialects"]) -> _Lexicon:
    """
    What a dialect's lexer reads beyond the standard. DEFAULT is the
    SQLite reading, which takes `[...]` and backtick identifiers.
    """
    if dialect is None:
        return _STANDARD
    name = dialect.name
    if name in ("POSTGRES", "DUCKDB"):
        return _Lexicon(False, False, False, True)
    if name == "MYSQL":
        return _Lexicon(True, True, False, False)
    if name in ("MSSQL", "DEFAULT"):
        return _Lexicon(False, False, True, False)
    return _Lexicon(False, False, False, False)


_WORD_RE = re.compile(r"[A-Za-z_\u0080-￿][A-Za-z_0-9$\u0080-￿]*")
_NUMBER_RE = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_PARAM_RE = re.compile(r"\$\d+|%\(\w+\)s|%s|\?|:[A-Za-z_]\w*")
_DOLLAR_RE = re.compile(r"\$(?P<tag>[A-Za-z_][A-Za-z_0-9]*)?\$")
_OP_RE = re.compile(r"::|<>|!=|<=|>=|\|\||->>|->|[=<>+\-*/%|&^~!@#:]")
_PUNCT = "(),;.[]"
# A prefix that makes the literal after it a string of another kind:
# E'...' (escapes), N'...' (national), X'...' and B'...' (bits and
# bytes), U&'...' (unicode escapes).
_PREFIX_RE = re.compile(r"(?:[EeNnXxBb]|[Uu]&)'")


def _quoted(
    sql: str, start: int, quote: str, backslash: bool
) -> Optional[Tuple[str, int]]:
    """
    Reads a quoted run that opens at `start`, where `quote` closes it and
    a doubled `quote` escapes it. Returns the contents with the escapes
    undone and the offset after the closing quote, or None when the run
    never closes.
    """
    out: List[str] = []
    i = start + 1
    end = len(sql)
    while i < end:
        char = sql[i]
        if backslash and char == "\\" and i + 1 < end:
            out.append(sql[i + 1])
            i += 2
            continue
        if char == quote:
            if i + 1 < end and sql[i + 1] == quote:
                out.append(quote)
                i += 2
                continue
            return "".join(out), i + 1
        out.append(char)
        i += 1
    return None


def tokenize(sql: str, dialect: Optional["Dialects"] = None) -> List[Token]:
    """
    Splits one statement into tokens, with comments and whitespace left
    out. The dialect decides the lexical rules that differ between
    engines; with none given, the standard rules apply with `[...]`
    identifiers read as SQLite reads them.

    Text the lexer cannot close, such as a literal with no closing quote,
    ends the list with one ERROR token that holds the rest of the
    statement.
    """
    lexicon = _lexicon(dialect)
    tokens: List[Token] = []
    i = 0
    end = len(sql)

    def error(at: int) -> List[Token]:
        tokens.append(Token(ERROR, sql[at:], sql[at:], at))
        return tokens

    while i < end:
        char = sql[i]
        if char.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            newline = sql.find("\n", i)
            i = end if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", i):
            close = sql.find("*/", i + 2)
            if close < 0:
                return error(i)
            i = close + 2
            continue
        prefix = _PREFIX_RE.match(sql, i)
        if prefix and not (i > 0 and (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            quote_at = prefix.end() - 1
            escapes = lexicon.backslash_escapes or sql[i] in "Ee"
            read = _quoted(sql, quote_at, "'", escapes)
            if read is None:
                return error(i)
            tokens.append(Token(STRING, sql[i : read[1]], read[0], i))
            i = read[1]
            continue
        if char == "'" or (char == '"' and lexicon.double_quoted_strings):
            read = _quoted(sql, i, char, lexicon.backslash_escapes)
            if read is None:
                return error(i)
            tokens.append(Token(STRING, sql[i : read[1]], read[0], i))
            i = read[1]
            continue
        if char in '"`':
            read = _quoted(sql, i, char, False)
            if read is None:
                return error(i)
            tokens.append(Token(IDENT, sql[i : read[1]], read[0], i))
            i = read[1]
            continue
        if char == "[" and lexicon.bracket_identifiers:
            close = sql.find("]", i + 1)
            # `]]` escapes a bracket inside the name.
            while close >= 0 and sql.startswith("]]", close):
                close = sql.find("]", close + 2)
            if close < 0:
                return error(i)
            value = sql[i + 1 : close].replace("]]", "]")
            tokens.append(Token(IDENT, sql[i : close + 1], value, i))
            i = close + 1
            continue
        if char == "$" and lexicon.dollar_quotes:
            opening = _DOLLAR_RE.match(sql, i)
            glued = i > 0 and (sql[i - 1].isalnum() or sql[i - 1] in "_$")
            if opening and not glued:
                delimiter = opening.group(0)
                close = sql.find(delimiter, opening.end())
                if close < 0:
                    return error(i)
                after = close + len(delimiter)
                body = sql[opening.end() : close]
                tokens.append(Token(STRING, sql[i:after], body, i))
                i = after
                continue
        word = _WORD_RE.match(sql, i)
        if word:
            text = word.group(0)
            tokens.append(Token(WORD, text, text.upper(), i))
            i = word.end()
            continue
        number = _NUMBER_RE.match(sql, i)
        if number:
            text = number.group(0)
            tokens.append(Token(NUMBER, text, text, i))
            i = number.end()
            continue
        param = _PARAM_RE.match(sql, i)
        if param and not sql.startswith("::", i):
            text = param.group(0)
            tokens.append(Token(PARAM, text, text, i))
            i = param.end()
            continue
        if char in _PUNCT:
            tokens.append(Token(PUNCT, char, char, i))
            i += 1
            continue
        op = _OP_RE.match(sql, i)
        if op:
            text = op.group(0)
            tokens.append(Token(OP, text, text, i))
            i = op.end()
            continue
        return error(i)
    return tokens
