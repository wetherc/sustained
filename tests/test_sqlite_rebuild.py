"""
The SQLite table rebuild, run against in-memory SQLite: each generated
migration is applied, and the schema it leaves is read back.
"""

import sqlite3
import unittest

from sustained import create_model
from sustained.autogenerate import autogenerate, diff_schema
from sustained.schema import Index, Integer, String, Text, Timestamp
from sustained.types import Expression


def model_of(columns, indexes=None, table="rb_items"):
    model = create_model(f"Rebuild_{table}", table)
    model.tableColumns = columns
    model.columns = tuple(columns)
    model.indexes = indexes or []
    return model


class RebuildTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE rb_items (id INTEGER PRIMARY KEY, code INTEGER, note TEXT)"
        )
        self.conn.execute("INSERT INTO rb_items VALUES (1, 7, 'first')")

    def apply(self, migration):
        for statement in migration.up:
            self.conn.execute(statement)

    def indexes(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'rb_items' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [row[0] for row in rows]

    def rows(self):
        return self.conn.execute("SELECT * FROM rb_items ORDER BY id").fetchall()


class TestRebuildWithIndexChanges(RebuildTestCase):
    def test_a_new_index_is_created_once(self):
        model = model_of(
            {"id": Integer(primary_key=True), "code": String(10), "note": Text()},
            [Index("ix_rb_code", "code")],
        )
        migration = autogenerate(self.conn, [model], id="m")
        creates = [s for s in migration.up if "CREATE INDEX" in s]
        self.assertEqual(len(creates), 1)
        self.apply(migration)
        self.assertEqual(self.indexes(), ["ix_rb_code"])
        self.assertTrue(diff_schema(self.conn, [model]).is_empty())

    def test_a_changed_index_comes_from_the_rebuild(self):
        self.conn.execute("CREATE INDEX ix_rb_code ON rb_items (code)")
        model = model_of(
            {"id": Integer(primary_key=True), "code": String(10), "note": Text()},
            [Index("ix_rb_code", "code", unique=True)],
        )
        migration = autogenerate(self.conn, [model], id="m")
        self.assertFalse(any(s.startswith("DROP INDEX") for s in migration.up))
        self.apply(migration)
        self.assertTrue(diff_schema(self.conn, [model]).is_empty())


class TestRebuildWithDrops(RebuildTestCase):
    def test_an_index_on_a_dropped_column_goes_with_the_table(self):
        self.conn.execute("CREATE INDEX ix_rb_note ON rb_items (note)")
        model = model_of({"id": Integer(primary_key=True), "code": String(10)})
        migration = autogenerate(self.conn, [model], id="m", allow_drops=True)
        self.assertFalse(any(s.startswith("DROP INDEX") for s in migration.up))
        self.apply(migration)
        self.assertEqual(self.indexes(), [])
        self.assertTrue(diff_schema(self.conn, [model]).is_empty())

    def test_an_undeclared_index_on_a_kept_column_is_dropped(self):
        self.conn.execute("CREATE INDEX ix_rb_code ON rb_items (code)")
        model = model_of(
            {"id": Integer(primary_key=True), "code": String(10), "note": Text()}
        )
        migration = autogenerate(self.conn, [model], id="m", allow_drops=True)
        self.apply(migration)
        self.assertEqual(self.indexes(), [])
        self.assertEqual(self.rows(), [(1, "7", "first")])

    def test_without_drops_the_undeclared_index_is_kept(self):
        self.conn.execute("CREATE INDEX ix_rb_code ON rb_items (code)")
        model = model_of(
            {"id": Integer(primary_key=True), "code": String(10), "note": Text()}
        )
        migration = autogenerate(self.conn, [model], id="m", ignore_undeclared=True)
        self.apply(migration)
        self.assertEqual(self.indexes(), ["ix_rb_code"])


class TestRebuildTightensToNotNull(RebuildTestCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("INSERT INTO rb_items VALUES (2, 8, NULL)")

    def test_the_default_fills_the_nulls(self):
        model = model_of(
            {
                "id": Integer(primary_key=True),
                "code": String(10),
                "note": Text(nullable=False, default="none"),
            }
        )
        self.apply(autogenerate(self.conn, [model], id="m"))
        self.assertEqual(self.rows(), [(1, "7", "first"), (2, "8", "none")])

    def test_the_backfill_wins_over_the_default(self):
        model = model_of(
            {
                "id": Integer(primary_key=True),
                "code": String(10),
                "note": Text(nullable=False, default="none", backfill="old"),
            }
        )
        self.apply(autogenerate(self.conn, [model], id="m"))
        self.assertEqual(self.rows()[1], (2, "8", "old"))

    def test_no_filler_refuses_before_any_statement(self):
        model = model_of(
            {
                "id": Integer(primary_key=True),
                "code": String(10),
                "note": Text(nullable=False),
            }
        )
        with self.assertRaisesRegex(ValueError, "rb_items.note' to NOT NULL"):
            autogenerate(self.conn, [model], id="m")


class TestAddColumnSqliteRefuses(RebuildTestCase):
    def columns(self, **extra):
        return {
            "id": Integer(primary_key=True),
            "code": Integer(),
            "note": Text(),
            **extra,
        }

    def assert_rebuilt(self, model):
        migration = autogenerate(self.conn, [model], id="m")
        self.assertFalse(any("ADD COLUMN" in s for s in migration.up))
        self.apply(migration)
        self.assertTrue(diff_schema(self.conn, [model]).is_empty())
        return migration

    def test_a_unique_column_rebuilds(self):
        self.assert_rebuilt(model_of(self.columns(sku=String(20, unique=True))))

    def test_a_timestamp_default_rebuilds(self):
        model = model_of(
            self.columns(seen=Timestamp(default=Expression("CURRENT_TIMESTAMP")))
        )
        self.assert_rebuilt(model)
        (seen,) = self.conn.execute("SELECT seen FROM rb_items").fetchone()
        self.assertIsNotNone(seen)

    def test_an_expression_in_parentheses_rebuilds(self):
        self.assert_rebuilt(
            model_of(self.columns(rank=Integer(default=Expression("(1 + 1)"))))
        )

    def test_a_reference_with_a_default_rebuilds(self):
        self.conn.execute("CREATE TABLE rb_owners (id INTEGER PRIMARY KEY)")
        self.conn.execute("INSERT INTO rb_owners VALUES (0)")
        owners = model_of({"id": Integer(primary_key=True)}, table="rb_owners")
        items = model_of(
            self.columns(owner_id=Integer(default=0, references="rb_owners.id"))
        )
        migration = autogenerate(self.conn, [owners, items], id="m")
        self.assertFalse(any("ADD COLUMN" in s for s in migration.up))

    def test_every_new_column_of_a_rebuilt_table_comes_from_the_rebuild(self):
        self.assert_rebuilt(
            model_of(self.columns(plain=Text(), sku=String(20, unique=True)))
        )

    def test_a_constant_default_takes_add_column(self):
        for default in (Expression("'x'"), Expression("-1.5"), Expression("NULL")):
            with self.subTest(default=str(default)):
                model = model_of(self.columns(extra=Text(default=default)))
                migration = autogenerate(self.conn, [model], id="m")
                self.assertEqual(len(migration.up), 1)
                self.assertIn("ADD COLUMN", migration.up[0])


class TestRebuildKeepsUndeclared(RebuildTestCase):
    def test_undeclared_columns_and_keys_keep_their_definitions(self):
        self.conn.execute("CREATE TABLE rb_owners (id INTEGER PRIMARY KEY)")
        self.conn.execute("INSERT INTO rb_owners VALUES (1)")
        self.conn.execute(
            "ALTER TABLE rb_items ADD COLUMN owner_id INTEGER "
            "CONSTRAINT fk_rb_owner REFERENCES rb_owners (id) "
            "ON DELETE CASCADE ON UPDATE SET NULL"
        )
        self.conn.execute(
            "ALTER TABLE rb_items ADD COLUMN tag TEXT NOT NULL DEFAULT 'none'"
        )
        model = model_of(
            {"id": Integer(primary_key=True), "code": String(10), "note": Text()}
        )
        owners = model_of({"id": Integer(primary_key=True)}, table="rb_owners")
        migration = autogenerate(
            self.conn, [model, owners], id="m", ignore_undeclared=True
        )
        self.apply(migration)
        (sql,) = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'rb_items'"
        ).fetchone()
        self.assertIn("\"tag\" TEXT NOT NULL DEFAULT 'none'", sql)
        self.assertIn("ON DELETE CASCADE ON UPDATE SET NULL", sql)


class TestRebuildCarriesWhatModelsCannotDeclare(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE rb_items (
                id INTEGER PRIMARY KEY,
                code INTEGER,
                name TEXT COLLATE NOCASE CHECK (length(name) < 10),
                legacy TEXT COLLATE RTRIM,
                CHECK (code > 0),
                UNIQUE (code, name)
            );
            CREATE TABLE rb_log (msg TEXT);
            CREATE TABLE rb_other (id INTEGER);
            CREATE VIEW rb_named AS SELECT name FROM rb_items;
            CREATE VIEW rb_upper AS SELECT upper(name) AS name FROM rb_named;
            CREATE TRIGGER rb_logged AFTER INSERT ON rb_items
            BEGIN INSERT INTO rb_log VALUES (new.name); END;
            CREATE TRIGGER rb_mirror AFTER INSERT ON rb_other
            BEGIN INSERT INTO rb_items (code, name) VALUES (new.id, 'mirror'); END;
            INSERT INTO rb_items (id, code, name, legacy) VALUES (1, 5, 'Ada', 'x');
            """)

    def model(self, **columns):
        return model_of(
            {
                "id": Integer(primary_key=True),
                "code": String(10),
                "name": Text(),
                **columns,
            }
        )

    def rebuild(self, model):
        migration = autogenerate(
            self.conn, [model], id="m", exclude_tables=("rb_log", "rb_other")
        )
        self.assertIn("PRAGMA legacy_alter_table = ON", migration.up)
        for statement in migration.up:
            self.conn.execute(statement)

    def refused(self, sql):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql)

    def test_views_read_the_rebuilt_table(self):
        self.rebuild(self.model(legacy=Text()))
        self.assertEqual(
            self.conn.execute("SELECT name FROM rb_upper").fetchall(), [("ADA",)]
        )

    def test_triggers_come_back(self):
        self.rebuild(self.model(legacy=Text()))
        self.conn.execute("INSERT INTO rb_items (code, name) VALUES (6, 'Bo')")
        self.conn.execute("INSERT INTO rb_other VALUES (7)")
        self.assertEqual(
            self.conn.execute("SELECT msg FROM rb_log").fetchall(),
            [("Ada",), ("Bo",), ("mirror",)],
        )

    def test_unnamed_checks_and_unique_come_back(self):
        self.rebuild(self.model(legacy=Text()))
        self.refused("INSERT INTO rb_items (code, name) VALUES (0, 'Cy')")
        self.refused("INSERT INTO rb_items (code, name) VALUES (8, 'much too long')")
        self.refused("INSERT INTO rb_items (code, name) VALUES (5, 'Ada')")

    def test_collations_come_back(self):
        self.rebuild(self.model(legacy=Text()))
        rows = self.conn.execute("SELECT id FROM rb_items WHERE name = 'ADA'")
        self.assertEqual(rows.fetchall(), [(1,)])
        (sql,) = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'rb_items'"
        ).fetchone()
        self.assertIn("COLLATE RTRIM", sql)

    def test_a_carried_check_goes_with_its_dropped_column(self):
        self.conn.execute("DROP VIEW rb_upper")
        self.conn.execute("DROP VIEW rb_named")
        self.conn.execute("DROP TRIGGER rb_logged")
        self.conn.execute("DROP TRIGGER rb_mirror")
        model = model_of({"id": Integer(primary_key=True), "code": String(10)})
        migration = autogenerate(self.conn, [model], id="m", allow_drops=True)
        self.assertNotIn("PRAGMA legacy_alter_table = ON", migration.up)
        for statement in migration.up:
            self.conn.execute(statement)
        self.refused("INSERT INTO rb_items (code) VALUES (0)")
        self.conn.execute("INSERT INTO rb_items (code) VALUES (5)")

    def test_a_renamed_column_is_renamed_in_what_is_carried(self):
        self.conn.execute("DROP VIEW rb_upper")
        self.conn.execute("DROP VIEW rb_named")
        model = model_of(
            {
                "id": Integer(primary_key=True),
                "code": String(10),
                "label": Text(),
                "legacy": Text(),
            }
        )
        migration = autogenerate(
            self.conn,
            [model],
            id="m",
            renames={"rb_items.name": "label"},
            exclude_tables=("rb_log", "rb_other"),
        )
        for statement in migration.up:
            self.conn.execute(statement)
        self.refused("INSERT INTO rb_items (code, label) VALUES (8, 'much too long')")
        self.conn.execute("INSERT INTO rb_items (code, label) VALUES (9, 'Di')")
        self.assertIn(("Di",), self.conn.execute("SELECT msg FROM rb_log").fetchall())


if __name__ == "__main__":
    unittest.main()
