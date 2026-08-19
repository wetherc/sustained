"""
The async surface, run against a real server through its async driver.
A server module subclasses AsyncCase, and the support page calls
what runs here `async`: AsyncMigrator applies, rehearses, and reverts,
arun() round-trips rows into models, and async_transaction() nests
through savepoints. Each server connects through the adapter named in
ADAPTERS, so asyncpg proves its $1..$n placeholder conversion here too.
"""

import importlib
import unittest

from sustained import ddl
from sustained.aio import async_transaction
from sustained.aio_migrations import AsyncMigrator
from sustained.dialects import Dialects
from sustained.migrations import Migration

from . import harness, lifecycle


def _async_driver(name, module):
    """The async driver module, or a skip naming what to install."""
    try:
        return importlib.import_module(module)
    except ImportError:
        harness._stop(name, f"the {module} driver is missing. Install {module}")


async def _adapter_postgres():
    from sustained.aio import AsyncpgAdapter

    asyncpg = _async_driver("postgres", "asyncpg")
    connection = await asyncpg.connect(harness.dsn("postgres"))
    return AsyncpgAdapter(connection), connection.close


async def _adapter_sqlite():
    from sustained.aio import AiosqliteAdapter

    aiosqlite = _async_driver("sqlite", "aiosqlite")
    connection = await aiosqlite.connect(harness.dsn("sqlite"))
    return AiosqliteAdapter(connection), connection.close


ADAPTERS = {
    "postgres": _adapter_postgres,
    "sqlite": _adapter_sqlite,
}


class AsyncCase(unittest.IsolatedAsyncioTestCase):
    """
    Base for one server's async run. Subclasses set NAME to a row in
    support.json and DIALECT to the dialect that row names.
    """

    NAME = ""
    DIALECT = Dialects.DEFAULT

    async def asyncSetUp(self):
        if not self.NAME:
            raise unittest.SkipTest("base class")
        self.adapter, self._close = await ADAPTERS[self.NAME]()
        self.Widget = lifecycle.make_model(
            "Widget", "it_widgets", self.DIALECT, lifecycle.widget_columns()
        )
        self.Widget.bind_async(self.adapter)
        await self.drop_everything()

    async def asyncTearDown(self):
        await self.drop_everything()
        self.Widget.unbind_async()
        await self._close()

    async def drop_everything(self):
        compiler = Dialects.get_compiler(self.DIALECT)
        for table in lifecycle.TABLES + lifecycle.TRACKING:
            await self.adapter.execute(
                f"DROP TABLE IF EXISTS {compiler.quote_identifier(table)}", ()
            )
        await self.adapter.commit()

    def migrator(self, migrations):
        return AsyncMigrator(self.adapter, list(migrations), dialect=self.DIALECT)

    def widget_migration(self):
        return Migration("0001_widgets", up=[ddl.create_table(self.Widget)])

    # The migrator

    async def test_the_migration_lands_and_comes_back(self):
        migrator = self.migrator([self.widget_migration()])
        applied = await migrator.up()
        self.assertEqual(["0001_widgets"], applied)
        self.assertEqual([("0001_widgets", "applied")], list(await migrator.statuses()))
        await self.Widget.query().insert({"id": 1, "name": "anvil"}).arun()

        reverted = await migrator.down()
        self.assertEqual(["0001_widgets"], reverted)
        self.assertEqual([("0001_widgets", "pending")], list(await migrator.statuses()))

    async def test_rehearsal_proves_the_run_and_leaves_nothing(self):
        migrator = self.migrator([self.widget_migration()])
        rehearsal = await migrator.rehearse()
        result = rehearsal[0]
        self.assertTrue(result.up_ok)
        self.assertTrue(result.down_ok)
        self.assertEqual([("0001_widgets", "pending")], list(await migrator.statuses()))
        with self.assertRaises(Exception):
            await self.Widget.query().arun()

    # The queries

    async def test_arun_round_trips_rows_into_models(self):
        await self.migrator([self.widget_migration()]).up()
        # A multi-row insert runs through executemany, whose row count
        # asyncpg does not report; the select below proves the rows landed.
        await self.Widget.query().insert(
            [
                {"id": 1, "name": "anvil", "size": 3},
                {"id": 2, "name": "bellows", "size": 7},
            ]
        ).arun()

        widgets = await self.Widget.query().orderBy("id").arun()
        self.assertEqual(["anvil", "bellows"], [w.name for w in widgets])
        self.assertIsInstance(widgets[0], self.Widget)

        updated = (
            await self.Widget.query()
            .update({"size": 9})
            .where("name", "=", "anvil")
            .arun()
        )
        self.assertEqual(1, updated)
        anvil = await self.Widget.query().where("id", "=", 1).afirst()
        self.assertEqual(9, anvil.size)

        deleted = await self.Widget.query().delete().where("id", "=", 2).arun()
        self.assertEqual(1, deleted)
        self.assertEqual(1, len(await self.Widget.query().arun()))

    async def test_a_literal_percent_s_survives_as_a_value(self):
        # asyncpg statements arrive with %s placeholders and convert to
        # $1..$n; the values themselves must pass through untouched.
        await self.migrator([self.widget_migration()]).up()
        await self.Widget.query().insert({"id": 1, "name": "100%s off"}).arun()
        found = await self.Widget.query().where("name", "=", "100%s off").afirst()
        self.assertEqual(1, found.id)

    # The transactions

    async def test_inner_failure_rolls_back_only_the_inner_block(self):
        await self.migrator([self.widget_migration()]).up()
        async with async_transaction(self.adapter, self.DIALECT):
            await self.Widget.query().insert({"id": 1, "name": "kept"}).arun()
            with self.assertRaises(RuntimeError):
                async with async_transaction(self.adapter, self.DIALECT):
                    await self.Widget.query().insert({"id": 2, "name": "gone"}).arun()
                    raise RuntimeError("boom")
        names = [w.name for w in await self.Widget.query().orderBy("id").arun()]
        self.assertEqual(["kept"], names)

    async def test_rollback_leaves_no_rows(self):
        await self.migrator([self.widget_migration()]).up()
        with self.assertRaises(RuntimeError):
            async with async_transaction(self.adapter, self.DIALECT):
                await self.Widget.query().insert({"id": 1, "name": "gone"}).arun()
                raise RuntimeError("boom")
        self.assertEqual([], await self.Widget.query().arun())
