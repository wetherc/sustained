"""
The generic query types live in builder.pyi, so only a type checker can see
them. These tests run mypy over a sample module and read what it inferred.
They are skipped when mypy is not installed.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

SAMPLE = """
from sustained import Model

class Show(Model):
    tableName = "shows"

reveal_type(Show.query())
reveal_type(Show.query().where("venue", "=", "Bowery").orderBy("night").run())
reveal_type(Show.query().first())
reveal_type(Show.query().limit(1).clone())
reveal_type(Show.query().to_dicts())
reveal_type(Show.query().delete().where("id", "=", 1))
reveal_type(Show.query().insert({"venue": "Bowery"}).returning("id").run())
reveal_type(Show.query().update({"venue": "Bowery"}).whereNull("venue").run())

count: int = Show.query().run()
"""


def run_mypy(source):
    """Type checks one module against the installed stubs, notes first."""
    from mypy import api

    with tempfile.TemporaryDirectory() as tmp:
        sample = Path(tmp) / "sample.py"
        sample.write_text(source)
        stdout, _, _ = api.run(
            ["--strict", "--no-incremental", "--no-error-summary", str(sample)]
        )
    return stdout


@unittest.skipUnless(
    importlib.util.find_spec("mypy") is not None, "mypy is not installed"
)
class TestQueryTypes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os

        os.environ["MYPYPATH"] = SRC
        cls.output = run_mypy(SAMPLE)

    def revealed(self, expected):
        """
        How many lines mypy revealed as this type. Optional and union types
        are spelled two ways across mypy releases, so both are counted.
        """
        old_style = expected.replace(" | ", ", ")
        spellings = (
            (expected, f"Union[{old_style}]") if "|" in expected else (expected,)
        )
        return sum(
            f'Revealed type is "{s}"' in line
            for line in self.output.splitlines()
            for s in spellings
        )

    def assert_revealed(self, expected, times=1):
        self.assertEqual(self.revealed(expected), times, self.output)

    def test_query_carries_the_model(self):
        self.assert_revealed("sustained.builder.QueryBuilder[sample.Show]", times=2)

    def test_run_returns_model_instances(self):
        self.assert_revealed("builtins.list[sample.Show]")

    def test_first_returns_one_instance_or_none(self):
        self.assert_revealed("sample.Show | None")

    def test_to_dicts_returns_dicts(self):
        self.assert_revealed("builtins.list[builtins.dict[builtins.str, Any]]")

    def test_a_write_is_a_write_builder(self):
        self.assert_revealed("sustained.builder.WriteBuilder[sample.Show]")

    def test_a_write_returns_a_row_count_or_returning_rows(self):
        self.assert_revealed(
            "builtins.int | builtins.list[builtins.dict[builtins.str, Any]]", times=2
        )

    def test_the_wrong_result_type_is_an_error(self):
        self.assertIn(
            "error: Incompatible types in assignment (expression has type "
            '"list[Show]", variable has type "int")',
            self.output,
        )


DRIVERS = """
import sqlite3

from sustained import Model
from sustained.pool import ConnectionPool

class Show(Model):
    tableName = "shows"

connection = sqlite3.connect(":memory:")
Show.bind(connection)
Show.query().run(connection)
Show.bind(ConnectionPool(lambda: sqlite3.connect(":memory:")))
Show.query().insert({"venue": "Bowery", "capacity": 575}).run()
Show.query().where("capacity", ">", 100).to_dicts(connection)
"""


@unittest.skipUnless(
    importlib.util.find_spec("mypy") is not None, "mypy is not installed"
)
class TestDriverTypes(unittest.TestCase):
    """
    A real driver connection has to satisfy the Connection protocol, or
    every call that takes one rejects the thing users actually have.
    """

    def test_a_sqlite3_connection_is_a_connection(self):
        import os

        os.environ["MYPYPATH"] = SRC
        self.assertNotIn("error:", run_mypy(DRIVERS))


if __name__ == "__main__":
    unittest.main()
