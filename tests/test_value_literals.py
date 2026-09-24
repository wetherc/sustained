"""
format_value() renders dates, timestamps, decimals, and bytes as literals,
so str(query), Literal(), and CASE results accept every type a filter does.
"""

import datetime
import sqlite3
import unittest
from decimal import Decimal

from sustained import QueryBuilder, create_model
from sustained.dialects import Dialects
from sustained.expressions import Func, Literal

Sale = create_model("LiteralSale", "sales")

DAY = datetime.date(2024, 5, 17)
MOMENT = datetime.datetime(2024, 5, 17, 12, 30, 45, 250000)
AWARE = datetime.datetime(2024, 5, 17, 12, 30, 45, tzinfo=datetime.timezone.utc)


def literal(dialect: Dialects, value: object) -> str:
    return Dialects.get_compiler(dialect).format_value(value)


class TestLiteralText(unittest.TestCase):
    def test_default_dialect_writes_iso_text(self):
        self.assertEqual(literal(Dialects.DEFAULT, DAY), "'2024-05-17'")
        self.assertEqual(
            literal(Dialects.DEFAULT, MOMENT), "'2024-05-17 12:30:45.250000'"
        )
        self.assertEqual(
            literal(Dialects.DEFAULT, AWARE), "'2024-05-17 12:30:45+00:00'"
        )

    def test_typed_dialects_write_the_type(self):
        cases = {
            Dialects.POSTGRES: ("TIMESTAMP", "TIMESTAMPTZ"),
            Dialects.DUCKDB: ("TIMESTAMP", "TIMESTAMPTZ"),
            Dialects.MYSQL: ("TIMESTAMP", "TIMESTAMP"),
            Dialects.PRESTO: ("TIMESTAMP", "TIMESTAMP"),
            Dialects.ATHENA: ("TIMESTAMP", "TIMESTAMP"),
        }
        for dialect, (naive, aware) in cases.items():
            with self.subTest(dialect=dialect.name):
                self.assertEqual(literal(dialect, DAY), "DATE '2024-05-17'")
                self.assertEqual(
                    literal(dialect, MOMENT),
                    f"{naive} '2024-05-17 12:30:45.250000'",
                )
                self.assertEqual(
                    literal(dialect, AWARE), f"{aware} '2024-05-17 12:30:45+00:00'"
                )

    def test_mssql_casts_the_text(self):
        self.assertEqual(literal(Dialects.MSSQL, DAY), "CAST('2024-05-17' AS DATE)")
        self.assertEqual(
            literal(Dialects.MSSQL, MOMENT),
            "CAST('2024-05-17 12:30:45.250000' AS DATETIME2)",
        )
        self.assertEqual(
            literal(Dialects.MSSQL, AWARE),
            "CAST('2024-05-17 12:30:45+00:00' AS DATETIMEOFFSET)",
        )

    def test_decimal_keeps_every_digit(self):
        for dialect in Dialects:
            with self.subTest(dialect=dialect.name):
                self.assertEqual(literal(dialect, Decimal("199.990")), "199.990")
                self.assertEqual(literal(dialect, Decimal("1E+3")), "1000")

    def test_non_finite_decimal_raises(self):
        for value in (Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Bind it as a parameter"):
                    literal(Dialects.DEFAULT, value)

    def test_bytes_per_dialect(self):
        expected = {
            Dialects.DEFAULT: "X'0001ff'",
            Dialects.MYSQL: "X'0001ff'",
            Dialects.PRESTO: "X'0001ff'",
            Dialects.POSTGRES: "decode('0001ff', 'hex')",
            Dialects.DUCKDB: "from_hex('0001ff')",
            Dialects.MSSQL: "0x0001ff",
        }
        for dialect, text in expected.items():
            with self.subTest(dialect=dialect.name):
                self.assertEqual(literal(dialect, b"\x00\x01\xff"), text)

    def test_other_types_still_raise(self):
        with self.assertRaises(TypeError):
            literal(Dialects.DEFAULT, datetime.time(12, 30))

    def test_str_of_a_query_with_a_date_filter(self):
        query = QueryBuilder(Sale, dialect=Dialects.POSTGRES).where("day", "=", DAY)
        self.assertEqual(
            str(query), 'SELECT * FROM "sales" WHERE "day" = DATE \'2024-05-17\''
        )

    def test_case_result_with_a_decimal(self):
        query = Sale.query().select_case(
            "rate", Decimal("0"), [("amount > 10", Decimal("0.15"))]
        )
        self.assertEqual(
            str(query),
            "SELECT CASE WHEN amount > 10 THEN 0.15 ELSE 0 END AS rate FROM sales",
        )


class TestEnginesReadTheLiterals(unittest.TestCase):
    def test_sqlite(self):
        connection = sqlite3.connect(":memory:")
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        values = [DAY, MOMENT, Decimal("1.25"), b"\x00\x01\xff"]
        row = connection.execute(
            "SELECT " + ", ".join(compiler.format_value(v) for v in values)
        ).fetchone()
        self.assertEqual(
            row, ("2024-05-17", "2024-05-17 12:30:45.250000", 1.25, b"\x00\x01\xff")
        )
        connection.close()

    def test_duckdb(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("duckdb is not installed")
        compiler = Dialects.get_compiler(Dialects.DUCKDB)
        values = [DAY, MOMENT, Decimal("1.25"), b"\x00\x01\xff"]
        connection = duckdb.connect()
        row = connection.execute(
            "SELECT " + ", ".join(compiler.format_value(v) for v in values)
        ).fetchone()
        self.assertEqual(row, (DAY, MOMENT, Decimal("1.25"), b"\x00\x01\xff"))
        connection.close()

    def test_literal_in_a_function_runs_on_sqlite(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE sales (id INTEGER, day TEXT)")
        connection.execute("INSERT INTO sales VALUES (1, NULL)")
        sql, params = (
            Sale.query()
            .select(Func("COALESCE", "day", Literal(DAY), alias="day"))
            .to_sql()
        )
        self.assertEqual(connection.execute(sql, params).fetchall(), [("2024-05-17",)])
        connection.close()


if __name__ == "__main__":
    unittest.main()
