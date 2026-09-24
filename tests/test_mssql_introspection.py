"""
SQL Server's information_schema read. data_type carries no length or
precision there, so the read puts them back from their own columns.
"""

import unittest
from unittest import mock

from sustained import create_model
from sustained.autogenerate import autogenerate
from sustained.dialects import Dialects
from sustained.introspect import MSSQL_CATALOG, _information_schema_plan, _sized_type
from sustained.schema import Integer, Numeric, String, Text


def read(rows):
    """Runs the MSSQL column read against the given column rows."""
    plan = _information_schema_plan(MSSQL_CATALOG)
    query = next(plan)
    try:
        plan.send(rows)
        while True:
            plan.send([])
    except StopIteration as stop:
        return query, stop.value


def row(name, data_type, length=None, precision=None, scale=None):
    return ("t", name, data_type, "YES", None, "dbo", None, length, precision, scale)


class TestTheRead(unittest.TestCase):
    def test_the_query_selects_the_parameters_last(self):
        query, _ = read([])
        self.assertIn(
            "c.collation_name, c.character_maximum_length, c.numeric_precision, "
            "c.numeric_scale FROM",
            query,
        )

    def test_lengths_and_precision_go_back_on_the_type(self):
        _, snapshot = read(
            [
                row("name", "nvarchar", 100),
                row("body", "nvarchar", -1),
                row("blob", "varbinary", -1),
                row("code", "char", 3),
                row("price", "decimal", None, 10, 2),
                row("ratio", "numeric", None, 5, None),
                row("n", "int", None, 10, 0),
                row("note", "ntext", 1073741823),
            ]
        )
        types = {n: c.raw_type for n, c in snapshot["t"].columns.items()}
        self.assertEqual(
            types,
            {
                "name": "nvarchar(100)",
                "body": "nvarchar(MAX)",
                "blob": "varbinary(MAX)",
                "code": "char(3)",
                "price": "decimal(10,2)",
                "ratio": "numeric(5,0)",
                "n": "int",
                "note": "ntext",
            },
        )

    def test_the_default_and_collation_are_not_read_as_mysql_extras(self):
        _, snapshot = read(
            [
                (
                    "t",
                    "grade",
                    "nvarchar",
                    "NO",
                    "(N'raw')",
                    "dbo",
                    "auto_increment",
                    20,
                    None,
                    None,
                )
            ]
        )
        column = snapshot["t"].columns["grade"]
        self.assertEqual(column.restated_default(), "(N'raw')")
        self.assertEqual(column.collation, "auto_increment")
        self.assertFalse(column.autoincrement)

    def test_a_row_without_the_parameters_keeps_the_bare_type(self):
        _, snapshot = read([("t", "name", "nvarchar", "YES", None, "dbo", None)])
        self.assertEqual(snapshot["t"].columns["name"].raw_type, "nvarchar")

    def test_a_type_without_a_length_stays_bare(self):
        self.assertEqual(_sized_type("nvarchar", None, None, None), "nvarchar")
        self.assertEqual(_sized_type("decimal", None, None, None), "decimal")


def model(**columns):
    built = create_model("SizedThing", "t")
    built.tableColumns = {"id": Integer(primary_key=True), **columns}
    built.columns = tuple(built.tableColumns)
    return built


def generate(built, rows):
    _, snapshot = read([row("id", "int", None, 10, 0)] + rows)
    snapshot["t"] = snapshot["t"]._replace(primary_key=("id",))
    with mock.patch("sustained.autogenerate.introspect_schema", return_value=snapshot):
        return autogenerate(None, [built], id="m", dialect=Dialects.MSSQL)


class TestTheDiff(unittest.TestCase):
    def test_a_longer_string_diffs_and_reverts_to_the_old_length(self):
        migration = generate(model(name=String(200)), [row("name", "nvarchar", 100)])
        self.assertIsNotNone(migration)
        self.assertIn(
            "ALTER TABLE [t] ALTER COLUMN [name] NVARCHAR(200) NULL", migration.up
        )
        self.assertIn(
            "ALTER TABLE [t] ALTER COLUMN [name] nvarchar(100) NULL", migration.down
        )

    def test_a_widened_column_restates_its_default_as_sql(self):
        migration = generate(
            model(grade=String(40, nullable=False, default="raw")),
            [("t", "grade", "nvarchar", "NO", "(N'raw')", "dbo", None, 20, None, None)],
        )
        self.assertIn("ALTER TABLE [t] ADD DEFAULT (N'raw') FOR [grade]", migration.up)
        self.assertIn(
            "ALTER TABLE [t] ADD DEFAULT (N'raw') FOR [grade]", migration.down
        )

    def test_a_changed_precision_diffs(self):
        migration = generate(
            model(price=Numeric(12, 4)), [row("price", "decimal", None, 10, 2)]
        )
        self.assertIsNotNone(migration)
        self.assertIn(
            "ALTER TABLE [t] ALTER COLUMN [price] NUMERIC(12, 4) NULL", migration.up
        )

    def test_text_and_a_sized_string_are_told_apart(self):
        migration = generate(model(body=Text()), [row("body", "nvarchar", 255)])
        self.assertIsNotNone(migration)

    def test_matching_sizes_diff_clean(self):
        migration = generate(
            model(name=String(100), body=Text(), price=Numeric(10, 2)),
            [
                row("name", "nvarchar", 100),
                row("body", "nvarchar", -1),
                row("price", "decimal", None, 10, 2),
            ],
        )
        self.assertIsNone(migration)


if __name__ == "__main__":
    unittest.main()
