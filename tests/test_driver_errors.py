"""Tests for sustained.driver_errors."""

import sqlite3
import unittest

from sustained.driver_errors import is_missing_table


class StateError(Exception):
    """A driver error that exposes its SQLSTATE as an attribute."""

    def __init__(self, message, **attributes):
        super().__init__(message)
        for name, value in attributes.items():
            setattr(self, name, value)


class TestIsMissingTable(unittest.TestCase):
    def test_sqlstate_attributes_name_a_missing_table(self):
        cases = {
            "psycopg": StateError("boom", sqlstate="42P01"),
            "psycopg2": StateError("boom", pgcode="42P01"),
            "odbc state": StateError("boom", sqlstate="42S02"),
        }
        for driver, error in cases.items():
            with self.subTest(driver=driver):
                self.assertTrue(is_missing_table(error))

    def test_leading_codes_name_a_missing_table(self):
        cases = {
            "pymysql": Exception(1146, "Table 'db.t' doesn't exist"),
            "pyodbc": Exception("42S02", "[42S02] boom"),
        }
        for driver, error in cases.items():
            with self.subTest(driver=driver):
                self.assertTrue(is_missing_table(error))

    def test_messages_name_a_missing_table(self):
        messages = [
            "no such table: sustained_migrations",
            'relation "sustained_migrations" does not exist',
            "Table 'db.sustained_migrations' doesn't exist",
            "line 1:15: Table 'hive.default.t' does not exist",
            "Catalog Error: Table with name t does not exist!",
            "Catalog Error: Schema with name ops does not exist!",
            "[42S02] Invalid object name 'sustained_migrations'. (208)",
            "TABLE_NOT_FOUND: line 1:15",
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(is_missing_table(Exception(message)))

    def test_a_real_sqlite_error_reads_as_missing(self):
        conn = sqlite3.connect(":memory:")
        with self.assertRaises(sqlite3.OperationalError) as caught:
            conn.execute("SELECT id FROM absent")
        conn.close()
        self.assertTrue(is_missing_table(caught.exception))

    def test_other_failures_do_not_read_as_missing(self):
        errors = [
            Exception("Cannot operate on a closed database."),
            Exception("permission denied for table sustained_migrations"),
            Exception('column "seq" does not exist'),
            StateError("boom", sqlstate="42501"),
            StateError("boom", pgcode=None),
            Exception(1142, "SELECT command denied to user"),
            Exception(),
        ]
        for error in errors:
            with self.subTest(error=repr(error)):
                self.assertFalse(is_missing_table(error))


if __name__ == "__main__":
    unittest.main()
