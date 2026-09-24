"""The package source parses on the oldest Python that pyproject.toml allows.

Python 3.12 (PEP 701) relaxed what an f-string replacement field may
contain. A field that spans lines, reuses the enclosing quote, or has a
backslash or comment parses on 3.12 and raises SyntaxError on import below
it. These checks read tokens that only 3.12 and later produce, so the
suite on a new interpreter finds the problem without a 3.9 run.
"""

import tempfile
import tokenize
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src" / "sustained"
FSTRING_START = getattr(tokenize, "FSTRING_START", None)


def quote_of(token):
    """The quote characters that open an f-string or string token."""
    text = token.string.lstrip("rRbBfFuU")
    return text[:3] if text[:3] in ('"""', "'''") else text[:1]


def pre_312_problems(path):
    """Every f-string field in the file that Python 3.9 to 3.11 refuses."""
    problems = []
    with path.open("rb") as handle:
        tokens = list(tokenize.tokenize(handle.readline))
    # One entry per open f-string: its quote and whether a field is open.
    stack = []
    for token in tokens:
        where = f"{path.name}:{token.start[0]}"
        if token.type == FSTRING_START:
            quote = quote_of(token)
            if stack and stack[-1][1] and quote in stack[-1][0]:
                problems.append(f"{where} reuses the enclosing quote")
            stack.append([quote, 0])
        elif token.type == tokenize.FSTRING_END:
            stack.pop()
        elif not stack:
            continue
        elif token.type == tokenize.OP and token.string == "{":
            stack[-1][1] += 1
        elif token.type == tokenize.OP and token.string == "}":
            stack[-1][1] -= 1
        elif stack[-1][1]:
            if token.type in (tokenize.NL, tokenize.NEWLINE):
                if len(stack[-1][0]) == 1:
                    problems.append(f"{where} breaks a line inside a field")
            elif token.type == tokenize.COMMENT:
                problems.append(f"{where} has a comment inside a field")
            elif token.type == tokenize.STRING:
                if "\\" in token.string:
                    problems.append(f"{where} has a backslash inside a field")
                if quote_of(token) in stack[-1][0]:
                    problems.append(f"{where} reuses the enclosing quote")
    return problems


@unittest.skipIf(FSTRING_START is None, "f-string tokens need Python 3.12")
class TestSourceParsesBelow312(unittest.TestCase):
    def problems_in(self, source):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "sample.py"
        path.write_text(source)
        return pre_312_problems(path)

    def test_no_f_string_field_needs_pep_701(self):
        problems = []
        for path in sorted(SOURCE.rglob("*.py")):
            problems.extend(pre_312_problems(path))
        self.assertEqual(problems, [])

    def test_a_string_split_inside_a_field_is_found(self):
        problems = self.problems_in(
            "".join(
                [
                    "x = f\"a {y or 'b '\n    'c'} d\"\n",
                    "z = f\"{'#' if y else '''\n'''}\"\n",
                ]
            )
        )
        self.assertEqual(len(problems), 1)
        self.assertIn(":1 breaks a line inside a field", problems[0])

    def test_quotes_comments_and_backslashes_are_found(self):
        found = self.problems_in(
            "a = f\"{x['k']}\"\n"
            'b = f"{x["k"]}"\n'
            'c = f"{f"{x}"}"\n'
            "d = f\"{'\\n'.join(x)}\"\n"
            'e = f"""{x  # note\n}"""\n'
        )
        problems = [p.split(" ", 1)[1] for p in found]
        self.assertEqual(
            problems,
            [
                "reuses the enclosing quote",
                "reuses the enclosing quote",
                "has a backslash inside a field",
                "has a comment inside a field",
            ],
        )


if __name__ == "__main__":
    unittest.main()
