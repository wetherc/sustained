"""Tests for the tokenizer the impact recognizer and the textual scan share."""

import unittest

from sustained.dialects import Dialects
from sustained.impact.tokens import (
    ERROR,
    IDENT,
    NUMBER,
    OP,
    PARAM,
    PUNCT,
    STRING,
    TOKEN_RE,
    WORD,
    Token,
    tokenize,
)


def kinds(sql, dialect=None):
    return [(t.kind, t.value) for t in tokenize(sql, dialect)]


class TokenizeTestCase(unittest.TestCase):
    def test_words_are_upper_cased_values_with_their_spelling_kept(self):
        tokens = tokenize("create Index ix_a")
        self.assertEqual([t.value for t in tokens], ["CREATE", "INDEX", "IX_A"])
        self.assertEqual([t.text for t in tokens], ["create", "Index", "ix_a"])
        self.assertTrue(all(t.kind == WORD for t in tokens))

    def test_offsets_point_at_the_token(self):
        sql = "ALTER  TABLE t"
        for token in tokenize(sql):
            self.assertEqual(
                sql[token.start : token.start + len(token.text)], token.text
            )

    def test_doubled_quote_escapes_a_literal(self):
        self.assertEqual(kinds("'it''s'"), [(STRING, "it's")])

    def test_backslash_is_plain_text_in_a_standard_literal(self):
        self.assertEqual(
            kinds(r"'a\' , 'b'"), [(STRING, "a\\"), (PUNCT, ","), (STRING, "b")]
        )

    def test_mysql_reads_a_backslash_escape(self):
        self.assertEqual(kinds(r"'it\'s'", Dialects.MYSQL), [(STRING, "it's")])

    def test_mysql_reads_double_quotes_as_a_string(self):
        self.assertEqual(kinds('"a\\"b"', Dialects.MYSQL), [(STRING, 'a"b')])

    def test_postgres_escape_string_reads_backslashes(self):
        self.assertEqual(kinds(r"E'a\'b'", Dialects.POSTGRES), [(STRING, "a'b")])

    def test_prefixed_literals(self):
        self.assertEqual(
            kinds("N'x' X'ff' U&'y'", Dialects.MSSQL),
            [(STRING, "x"), (STRING, "ff"), (STRING, "y")],
        )

    def test_a_prefix_glued_to_a_word_is_part_of_the_word(self):
        self.assertEqual(kinds("men'x'"), [(WORD, "MEN"), (STRING, "x")])

    def test_quoted_identifiers(self):
        self.assertEqual(
            kinds('"Order" "a""b" `c`'),
            [(IDENT, "Order"), (IDENT, 'a"b'), (IDENT, "c")],
        )

    def test_bracket_identifiers_on_mssql_and_sqlite(self):
        self.assertEqual(kinds("[a]]b]", Dialects.MSSQL), [(IDENT, "a]b")])
        self.assertEqual(kinds("[users]"), [(IDENT, "users")])
        self.assertEqual(kinds("[users]", Dialects.DEFAULT), [(IDENT, "users")])

    def test_brackets_are_punctuation_on_postgres(self):
        self.assertEqual(
            kinds("a[1]", Dialects.POSTGRES),
            [(WORD, "A"), (PUNCT, "["), (NUMBER, "1"), (PUNCT, "]")],
        )

    def test_comments_are_left_out(self):
        self.assertEqual(
            kinds("a -- DROP TABLE x\n/* DROP */ b"), [(WORD, "A"), (WORD, "B")]
        )

    def test_a_comment_marker_inside_a_literal_is_text(self):
        self.assertEqual(kinds("'-- x /* y */'"), [(STRING, "-- x /* y */")])

    def test_dollar_quotes(self):
        self.assertEqual(
            kinds("$$a;'b$$ $fn$ $$ inner $$ $fn$", Dialects.POSTGRES),
            [(STRING, "a;'b"), (STRING, " $$ inner $$ ")],
        )

    def test_dollar_parameters_and_words_are_not_quotes(self):
        self.assertEqual(
            kinds("$1 a$b$c", Dialects.POSTGRES), [(PARAM, "$1"), (WORD, "A$B$C")]
        )

    def test_other_parameters(self):
        self.assertEqual(
            kinds("? %s %(n)s :name"),
            [(PARAM, "?"), (PARAM, "%s"), (PARAM, "%(n)s"), (PARAM, ":name")],
        )

    def test_casts_and_operators(self):
        self.assertEqual(
            kinds("a::int <> 1.5e3", Dialects.POSTGRES),
            [(WORD, "A"), (OP, "::"), (WORD, "INT"), (OP, "<>"), (NUMBER, "1.5e3")],
        )

    def test_unterminated_input_ends_in_one_error_token(self):
        for sql, dialect in [
            ("SELECT 'open", None),
            ('SELECT "open', None),
            ("SELECT /* open", None),
            ("SELECT $$ open", Dialects.POSTGRES),
            ("SELECT [open", Dialects.MSSQL),
        ]:
            with self.subTest(sql=sql):
                tokens = tokenize(sql, dialect)
                self.assertEqual(tokens[0], Token(WORD, "SELECT", "SELECT", 0))
                self.assertEqual(tokens[-1].kind, ERROR)
                self.assertEqual(tokens[-1].text, sql[7:])

    def test_a_character_no_rule_reads_is_an_error(self):
        self.assertEqual(kinds("a \x00 b")[-1], (ERROR, "\x00 b"))

    def test_token_helpers(self):
        word, ident, number = tokenize('ADD "Col" 1')
        self.assertTrue(word.is_word())
        self.assertTrue(word.is_word("ADD", "DROP"))
        self.assertFalse(word.is_word("DROP"))
        self.assertFalse(ident.is_word())
        self.assertEqual(word.name, "ADD")
        self.assertEqual(ident.name, "Col")
        self.assertIsNone(number.name)


class TokenPatternTestCase(unittest.TestCase):
    def test_the_scan_pattern_reads_dollar_quotes_with_and_without_a_tag(self):
        found = [m.group(0) for m in TOKEN_RE.finditer("$$ a $$ x $t$ b $t$ a$b$c")]
        self.assertEqual(found, ["$$ a $$", "$t$ b $t$"])


if __name__ == "__main__":
    unittest.main()
