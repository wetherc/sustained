"""
A multi-row insert renders its statement from the first row alone and
binds every row to it. The template is a shallow copy, so a large insert
does not copy its rows to render one.
"""

import sqlite3
import unittest
from unittest import mock

from sustained import create_model
from sustained.aio import DbApiAsyncAdapter
from sustained.schema import Integer, Text

Row = create_model("BatchTemplateRow", "batch_rows")
Row.tableColumns = {"id": Integer(primary_key=True), "name": Text()}
Row.columns = ("id", "name")

ROWS = [{"id": i, "name": f"n{i}"} for i in range(5)]


def no_deep_copy(*_):
    raise AssertionError("a batch insert deep-copied the query")


class BatchInsertTemplateTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        Row.create_table(self.conn)

    def tearDown(self):
        self.conn.close()

    def _names(self):
        return [r[0] for r in self.conn.execute("SELECT name FROM batch_rows")]

    def test_the_template_leaves_the_query_whole(self):
        query = Row.query().insert(ROWS)
        sql = query._first_row_sql()
        self.assertEqual(sql, Row.query().insert(ROWS[0]).to_sql()[0])
        self.assertEqual(len(query._insert_rows), 5)

    def test_a_batch_insert_copies_no_rows(self):
        with mock.patch("copy.deepcopy", side_effect=no_deep_copy):
            count = Row.query().insert(ROWS).run(self.conn)
        self.assertEqual(count, 5)
        self.assertEqual(self._names(), [r["name"] for r in ROWS])

    async def test_an_async_batch_insert_copies_no_rows(self):
        adapter = DbApiAsyncAdapter(self.conn)
        with mock.patch("copy.deepcopy", side_effect=no_deep_copy):
            count = await Row.query().insert(ROWS).arun(adapter)
        self.assertEqual(count, 5)
        self.assertEqual(self._names(), [r["name"] for r in ROWS])


if __name__ == "__main__":
    unittest.main()
