"""
Tests that a view stays out of the snapshot. A view's columns live in
information_schema.columns beside a table's, so an unfiltered read makes
every plan report a table the models do not declare, and allow_drops
emits a DROP TABLE the engine refuses.
"""

import unittest

from sustained.dialects import Dialects
from sustained.introspect import introspect_schema

COLUMNS = {
    "widgets": [
        ("widgets", "id", "int", "NO", None),
        ("widgets", "name", "varchar(20)", "YES", None),
    ],
    "widget_summary": [("widget_summary", "total", "int", "YES", None)],
}

VIEWS = ("widget_summary",)

BASE_TABLE_FILTER = "t.table_type = 'BASE TABLE'"


class FilteringCursor:
    """
    Serves catalog rows and honors the BASE TABLE filter itself, so a read
    that forgets the filter sees the view.
    """

    def __init__(self):
        self.statements = []
        self._current = []

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        self.statements.append(flat)
        if "information_schema.columns" not in flat:
            self._current = []
            return
        rows = []
        for table, table_rows in COLUMNS.items():
            if table in VIEWS and BASE_TABLE_FILTER in flat:
                continue
            rows.extend(table_rows)
        self._current = rows

    def fetchall(self):
        return list(self._current)


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class TestViewsStayOutOfTheSnapshot(unittest.TestCase):
    def read(self, dialect):
        cursor = FilteringCursor()
        return introspect_schema(FakeConnection(cursor), dialect), cursor

    def test_the_shared_read_sees_tables_only(self):
        for dialect in (
            Dialects.MYSQL,
            Dialects.MSSQL,
            Dialects.DUCKDB,
            Dialects.PRESTO,
            Dialects.ATHENA,
        ):
            with self.subTest(dialect=dialect):
                schema, _ = self.read(dialect)
                self.assertEqual(["widgets"], sorted(schema))

    def test_the_shared_read_keeps_its_schema_filter(self):
        _, cursor = self.read(Dialects.MYSQL)
        self.assertIn("c.table_schema = DATABASE()", cursor.statements[0])
