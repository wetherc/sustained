"""
first() and afirst() cap the query at one row in a form the dialect runs.
MSSQL writes LIMIT as OFFSET ... FETCH, which needs an ORDER BY, so a query
there with no ORDER BY takes TOP 1.
"""

import asyncio
import unittest

from sustained import DialectError, QueryBuilder, create_model
from sustained.aio import AsyncAdapter
from sustained.dialects import Dialects

Row = create_model("FirstRowCapRow", "rows")


class RecordingCursor:
    description = [("id",)]
    rowcount = -1

    def __init__(self, sent):
        self.sent = sent

    def execute(self, sql, params=()):
        self.sent.append(sql)

    def fetchall(self):
        return [(1,)]

    def close(self):
        pass


class RecordingConnection:
    def __init__(self):
        self.sent = []

    def cursor(self):
        return RecordingCursor(self.sent)

    def commit(self):
        pass


class RecordingAdapter(AsyncAdapter):
    def __init__(self):
        self.sent = []

    async def fetch(self, sql, params):
        self.sent.append(sql)
        return ["id"], [(1,)]

    async def execute(self, sql, params):
        return 0

    async def executemany(self, sql, seq):
        return 0

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def close(self):
        pass


def mssql():
    return QueryBuilder(Row, dialect=Dialects.MSSQL)


class TestFirstRowCap(unittest.TestCase):
    def test_mssql_without_order_by_takes_top(self):
        self.assertEqual(str(mssql()._first_query()), "SELECT TOP 1 * FROM [rows]")

    def test_mssql_with_order_by_keeps_offset_fetch(self):
        self.assertEqual(
            str(mssql().orderBy("id")._first_query()),
            "SELECT * FROM [rows] ORDER BY [id] ASC "
            "OFFSET 0 ROWS FETCH NEXT 1 ROWS ONLY",
        )

    def test_mssql_offset_without_order_by_still_refuses(self):
        with self.assertRaises(DialectError):
            str(mssql().offset(2)._first_query())

    def test_mssql_union_keeps_the_limit_refusal(self):
        query = mssql().union(mssql())
        with self.assertRaises(DialectError):
            str(query._first_query())

    def test_an_existing_cap_is_kept(self):
        self.assertEqual(
            str(mssql().top(3)._first_query()), "SELECT TOP 3 * FROM [rows]"
        )

    def test_other_dialects_use_limit(self):
        self.assertEqual(
            str(QueryBuilder(Row)._first_query()), "SELECT * FROM rows LIMIT 1"
        )

    def test_first_leaves_the_query_alone(self):
        query = mssql()
        connection = RecordingConnection()
        row = query.first(connection)
        self.assertEqual(row.id, 1)
        self.assertEqual(connection.sent, ["SELECT TOP 1 * FROM [rows]"])
        self.assertEqual(str(query), "SELECT * FROM [rows]")

    def test_afirst_takes_top_on_mssql(self):
        adapter = RecordingAdapter()
        row = asyncio.run(mssql().afirst(adapter))
        self.assertEqual(row.id, 1)
        self.assertEqual(adapter.sent, ["SELECT TOP 1 * FROM [rows]"])


if __name__ == "__main__":
    unittest.main()
