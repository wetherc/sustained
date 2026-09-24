"""
A run that fails part way lists the migrations it already applied on the
exception's `applied` attribute, whatever the exception's class.
"""

import sqlite3
import unittest

from sustained.aio import DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.migrations import Migration, Migrator


def migrations():
    return [
        Migration("001_a", up="CREATE TABLE a (id INTEGER)", down="DROP TABLE a"),
        Migration("002_b", up="CREATE TABLE b (id INTEGER)", down="DROP TABLE b"),
        Migration("003_bad", up="NOT SQL"),
    ]


class SlottedError(Exception):
    __slots__ = ()


def refuse(connection):
    raise SlottedError("refused")


class TestSyncUp(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_a_driver_error_lists_what_applied(self):
        with self.assertRaises(sqlite3.OperationalError) as caught:
            Migrator(self.conn, migrations()).up()
        self.assertEqual(caught.exception.applied, ["001_a", "002_b"])
        self.assertEqual(caught.exception.migration_id, "003_bad")

    def test_a_failure_on_the_first_migration_lists_nothing(self):
        with self.assertRaises(sqlite3.OperationalError) as caught:
            Migrator(self.conn, migrations()[2:]).up()
        self.assertFalse(hasattr(caught.exception, "applied"))

    def test_a_failing_repeatable_lists_the_versioned_run(self):
        view = Migration("v_bad", up="NOT SQL", repeatable=True)
        with self.assertRaises(sqlite3.OperationalError) as caught:
            Migrator(self.conn, migrations()[:2] + [view]).up()
        self.assertEqual(caught.exception.applied, ["001_a", "002_b"])

    def test_an_error_that_refuses_attributes_still_propagates(self):
        step = Migration("002_refuse", up=refuse, checksum="fixed")
        with self.assertRaises(SlottedError):
            Migrator(self.conn, migrations()[:1] + [step]).up()


class TestAsyncUp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.conn.close)
        self.adapter = DbApiAsyncAdapter(self.conn)

    async def test_a_driver_error_lists_what_applied(self):
        with self.assertRaises(sqlite3.OperationalError) as caught:
            await AsyncMigrator(self.adapter, migrations()).up()
        self.assertEqual(caught.exception.applied, ["001_a", "002_b"])

    async def test_a_failure_on_the_first_migration_lists_nothing(self):
        with self.assertRaises(sqlite3.OperationalError) as caught:
            await AsyncMigrator(self.adapter, migrations()[2:]).up()
        self.assertFalse(hasattr(caught.exception, "applied"))
