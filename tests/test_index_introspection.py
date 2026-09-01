"""
Tests for reading plain indexes on the engines whose shared
information_schema read cannot see them: MySQL and MariaDB through
information_schema.statistics, MSSQL through sys.indexes, and DuckDB
through duckdb_indexes(). Without these reads, a model's declared index
reads as missing on every plan, and the second run fails creating it
again.
"""

import unittest

from sustained.autogenerate import SchemaDiff, _diff_indexes
from sustained.dialects import Dialects
from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    _duckdb_index_columns,
    introspect_schema,
)
from sustained.model import Model
from sustained.schema import Index, Integer, String


class FakeCursor:
    """Serves canned catalog rows, keyed by a substring of the SQL."""

    def __init__(self, responses):
        self.responses = responses
        self.statements = []
        self._current = []

    def execute(self, sql, params=()):
        self.statements.append(" ".join(sql.split()))
        for fragment, rows in self.responses.items():
            if fragment in sql:
                if rows is None:
                    raise RuntimeError(f"no {fragment} here")
                self._current = rows
                return
        self._current = []

    def fetchall(self):
        return list(self._current)

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


COLUMNS = [
    ("t", "id", "int", "NO", None),
    ("t", "a", "int", "YES", None),
    ("t", "b", "int", "YES", None),
]


class TestMysqlIndexRead(unittest.TestCase):
    def read(self, statistics):
        cursor = FakeCursor(
            {
                "information_schema.columns": COLUMNS,
                "information_schema.statistics": statistics,
            }
        )
        return introspect_schema(FakeConnection(cursor), Dialects.MYSQL)

    def test_a_plain_index_is_read(self):
        schema = self.read([("t", "idx_a", 1, "a")])
        self.assertEqual(IntrospectedIndex(("a",), False), schema["t"].indexes["idx_a"])

    def test_a_unique_index_reports_unique(self):
        schema = self.read([("t", "uq_a", 0, "a")])
        self.assertEqual(IntrospectedIndex(("a",), True), schema["t"].indexes["uq_a"])

    def test_a_multi_column_index_keeps_its_order(self):
        schema = self.read([("t", "idx_ab", 1, "a"), ("t", "idx_ab", 1, "b")])
        self.assertEqual(("a", "b"), schema["t"].indexes["idx_ab"].columns)

    def test_the_primary_key_row_is_not_an_index(self):
        schema = self.read([("t", "PRIMARY", 0, "id")])
        self.assertEqual({}, dict(schema["t"].indexes))

    def test_a_functional_index_part_is_skipped(self):
        schema = self.read([("t", "idx_expr", 1, None)])
        self.assertEqual({}, dict(schema["t"].indexes))

    def test_a_missing_statistics_view_degrades_to_no_indexes(self):
        schema = self.read(None)
        self.assertEqual({}, dict(schema["t"].indexes))


class TestMssqlIndexRead(unittest.TestCase):
    def read(self, index_rows):
        cursor = FakeCursor(
            {
                "information_schema.columns": COLUMNS,
                "sys.indexes": index_rows,
            }
        )
        return introspect_schema(FakeConnection(cursor), Dialects.MSSQL)

    def test_a_plain_index_is_read(self):
        schema = self.read([("t", "idx_a", False, "a")])
        self.assertEqual(IntrospectedIndex(("a",), False), schema["t"].indexes["idx_a"])

    def test_a_create_unique_index_reports_unique(self):
        schema = self.read([("t", "uq_ab", True, "a"), ("t", "uq_ab", True, "b")])
        self.assertEqual(
            IntrospectedIndex(("a", "b"), True), schema["t"].indexes["uq_ab"]
        )

    def test_missing_sys_views_degrade_to_no_indexes(self):
        schema = self.read(None)
        self.assertEqual({}, dict(schema["t"].indexes))


class TestDuckdbExpressionParse(unittest.TestCase):
    def test_bare_columns_parse(self):
        self.assertEqual(("a", "b"), _duckdb_index_columns("[a, b]"))

    def test_an_expression_part_reads_as_none(self):
        self.assertIsNone(_duckdb_index_columns("[(a + b)]"))

    def test_an_empty_list_reads_as_none(self):
        self.assertIsNone(_duckdb_index_columns("[]"))


class TestDuckdbIndexRead(unittest.TestCase):
    def setUp(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("the duckdb driver is missing")
        self.connection = duckdb.connect(":memory:")

    def tearDown(self):
        self.connection.close()

    def test_a_created_index_is_read_back(self):
        self.connection.execute("CREATE TABLE t (a INT, b INT)")
        self.connection.execute("CREATE INDEX idx_a ON t (a)")
        self.connection.execute("CREATE UNIQUE INDEX uq_b ON t (b)")
        schema = introspect_schema(self.connection, Dialects.DUCKDB)
        self.assertEqual(IntrospectedIndex(("a",), False), schema["t"].indexes["idx_a"])
        self.assertEqual(IntrospectedIndex(("b",), True), schema["t"].indexes["uq_b"])

    def test_a_model_with_an_index_applies_twice(self):
        """The second up() must not recreate an index that already exists."""
        from sustained.migrations import Migrator

        model = type(
            "T",
            (Model,),
            {
                "tableName": "t",
                "tableColumns": {"id": Integer(primary_key=True), "a": Integer()},
                "indexes": [Index("idx_a", "a")],
                "_dialect": Dialects.DUCKDB,
            },
        )
        migrator = Migrator(self.connection, [], dialect=Dialects.DUCKDB)
        migrator.up(models=[model])
        self.assertIsNone(migrator.plan([model]))
        self.assertEqual([], migrator.up(models=[model]))


class TestForeignKeyBackingIndexes(unittest.TestCase):
    def test_an_index_named_after_a_foreign_key_is_not_an_extra(self):
        model = type(
            "T",
            (Model,),
            {
                "tableName": "t",
                "tableColumns": {
                    "id": Integer(primary_key=True),
                    "other_id": Integer(),
                    "name": String(40),
                },
            },
        )
        table = IntrospectedTable(
            columns={
                "id": IntrospectedColumn("int", False, True, None),
                "other_id": IntrospectedColumn("int", True, False, None),
                "name": IntrospectedColumn("varchar(40)", True, False, None),
            },
            primary_key=("id",),
            foreign_keys={
                "fk_other": IntrospectedForeignKey(
                    columns=("other_id",), target_table="other"
                )
            },
            indexes={
                "fk_other": IntrospectedIndex(("other_id",), False),
                "idx_stray": IntrospectedIndex(("name",), False),
            },
        )
        diff = SchemaDiff()
        _diff_indexes(diff, model, table)
        extras = [name for _, name, _ in diff.extra_indexes]
        self.assertEqual(["idx_stray"], extras)
