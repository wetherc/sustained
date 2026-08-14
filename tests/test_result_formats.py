"""
Tests for alternate result formats: dicts, pandas, and pyarrow.
"""

import sqlite3
import unittest

from sustained import Model

try:
    import pandas  # noqa: F401

    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

try:
    import pyarrow  # noqa: F401

    HAS_PYARROW = True
except ImportError:
    HAS_PYARROW = False


class FmtUser(Model):
    tableName = "users"


class ResultFormatTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        FmtUser.bind(self.conn)
        FmtUser.query().insert(
            [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}]
        ).run()

    def tearDown(self):
        FmtUser.unbind()
        self.conn.close()


class TestToDicts(ResultFormatTestCase):
    def test_rows_as_dicts(self):
        rows = FmtUser.query().orderBy("id").to_dicts()
        self.assertEqual(rows, [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}])

    def test_empty_result(self):
        rows = FmtUser.query().where("id", "=", 99).to_dicts()
        self.assertEqual(rows, [])

    def test_only_select_statements(self):
        with self.assertRaises(ValueError):
            FmtUser.query().delete().where("id", "=", 1).to_dicts()


@unittest.skipUnless(HAS_PANDAS, "pandas not installed")
class TestToDf(ResultFormatTestCase):
    def test_dataframe_shape_and_columns(self):
        df = FmtUser.query().orderBy("id").to_df()
        self.assertEqual(list(df.columns), ["id", "name"])
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0]["name"], "Ada")

    def test_empty_dataframe_keeps_columns(self):
        df = FmtUser.query().where("id", "=", 99).to_df()
        self.assertEqual(list(df.columns), ["id", "name"])
        self.assertEqual(len(df), 0)


@unittest.skipUnless(HAS_PYARROW, "pyarrow not installed")
class TestToArrow(ResultFormatTestCase):
    def test_table_columns_and_rows(self):
        table = FmtUser.query().orderBy("id").to_arrow()
        self.assertEqual(table.column_names, ["id", "name"])
        self.assertEqual(table.num_rows, 2)


class TestMissingDependencyMessages(ResultFormatTestCase):
    @unittest.skipIf(HAS_PANDAS, "pandas installed")
    def test_to_df_without_pandas(self):
        with self.assertRaises(RuntimeError):
            FmtUser.query().to_df()

    @unittest.skipIf(HAS_PYARROW, "pyarrow installed")
    def test_to_arrow_without_pyarrow(self):
        with self.assertRaises(RuntimeError):
            FmtUser.query().to_arrow()


if __name__ == "__main__":
    unittest.main()
