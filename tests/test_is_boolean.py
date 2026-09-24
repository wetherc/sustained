"""
where(column, "IS", True) and its three siblings write the truth value into
the SQL. A bound value after IS is a syntax error on Postgres and DuckDB.
"""

import sqlite3
import unittest

from sustained import QueryBuilder, create_model
from sustained.dialects import Dialects

Flag = create_model("IsBooleanFlag", "flags")


def to_sql(dialect: Dialects, operator: str, value: bool):
    return QueryBuilder(Flag, dialect=dialect).where("on", operator, value).to_sql()


class TestIsBoolean(unittest.TestCase):
    def test_keyword_dialects_write_the_keyword(self):
        for dialect, column in (
            (Dialects.POSTGRES, '"on"'),
            (Dialects.DUCKDB, '"on"'),
            (Dialects.MYSQL, "`on`"),
        ):
            with self.subTest(dialect=dialect.name):
                sql, params = to_sql(dialect, "IS", True)
                self.assertTrue(sql.endswith(f"WHERE {column} IS TRUE"), sql)
                self.assertEqual(params, ())
                sql, _ = to_sql(dialect, "is not", False)
                self.assertTrue(sql.endswith(f"WHERE {column} IS NOT FALSE"), sql)

    def test_presto_and_athena_use_distinct_from(self):
        for dialect in (Dialects.PRESTO, Dialects.ATHENA):
            with self.subTest(dialect=dialect.name):
                sql, params = to_sql(dialect, "IS", True)
                self.assertTrue(sql.endswith('"on" IS NOT DISTINCT FROM TRUE'), sql)
                self.assertEqual(params, ())
                sql, _ = to_sql(dialect, "IS NOT", False)
                self.assertTrue(sql.endswith('"on" IS DISTINCT FROM FALSE'), sql)

    def test_mssql_compares_the_bit_and_tests_null(self):
        sql, params = to_sql(Dialects.MSSQL, "IS", True)
        self.assertTrue(sql.endswith("WHERE ([on] IS NOT NULL AND [on] = 1)"), sql)
        self.assertEqual(params, ())
        sql, _ = to_sql(Dialects.MSSQL, "IS NOT", False)
        self.assertTrue(sql.endswith("WHERE ([on] IS NULL OR [on] <> 0)"), sql)

    def test_equals_true_still_binds(self):
        sql, params = to_sql(Dialects.POSTGRES, "=", True)
        self.assertTrue(sql.endswith('WHERE "on" = %s'), sql)
        self.assertEqual(params, (True,))

    def test_sqlite_reads_null_as_neither_value(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE flags (id INTEGER, flag BOOLEAN)")
        connection.executemany(
            "INSERT INTO flags VALUES (?, ?)", [(1, True), (2, False), (3, None)]
        )
        expected = {
            ("IS", True): [1],
            ("IS", False): [2],
            ("IS NOT", True): [2, 3],
            ("IS NOT", False): [1, 3],
        }
        for (operator, value), ids in expected.items():
            with self.subTest(operator=operator, value=value):
                sql, params = (
                    Flag.query().select("id").where("flag", operator, value).to_sql()
                )
                rows = connection.execute(sql + " ORDER BY id", params).fetchall()
                self.assertEqual([row[0] for row in rows], ids)
        connection.close()


if __name__ == "__main__":
    unittest.main()
