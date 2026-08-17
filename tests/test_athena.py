"""Tests for the Athena dialect: SQL rendering, DDL, and migrations."""

import unittest

from sustained import Model, create_model
from sustained.dialects import Dialects
from sustained.exceptions import DialectError
from sustained.migrations import Migration, Migrator
from sustained.schema import (
    Boolean,
    Date,
    Float,
    Index,
    Integer,
    Json,
    Numeric,
    String,
    TableOptions,
    Text,
    Timestamp,
)


def athena_model(name, table, columns, **attrs):
    model = create_model(name, table)
    model.tableColumns = columns
    model.columns = tuple(columns)
    for key, value in attrs.items():
        setattr(model, key, value)
    model.set_dialect(Dialects.ATHENA)
    return model


class TestAthenaQueries(unittest.TestCase):
    def setUp(self):
        self.Event = athena_model(
            f"AthQ_{self.id().rsplit('.', 1)[-1]}",
            "events",
            {"id": Integer(), "name": String(120)},
        )

    def test_placeholder_is_pyformat(self):
        sql, params = self.Event.query().where("name", "=", "x").to_sql()
        self.assertIn("%s", sql)
        self.assertNotIn("?", sql)
        self.assertEqual(params, ("x",))

    def test_offset_renders_before_limit(self):
        sql = str(self.Event.query().limit(5).offset(10))
        self.assertIn("OFFSET 10 LIMIT 5", sql)

    def test_identifiers_quote_with_double_quotes(self):
        sql = str(self.Event.query().select("name"))
        self.assertIn('"events"', sql)
        self.assertIn('"name"', sql)

    def test_ilike_lowercases_both_sides(self):
        sql = str(self.Event.query().whereILike("name", "a%"))
        self.assertIn("LOWER", sql)

    def test_now_renders_natively(self):
        sql = str(self.Event.query().now(alias="ts"))
        self.assertIn("NOW()", sql)

    def test_getdate_translates_to_now(self):
        sql = str(self.Event.query().getdate(alias="ts"))
        self.assertIn("NOW()", sql)

    def test_merge_upsert(self):
        sql = str(
            self.Event.query().insert({"id": 1, "name": "a"}).onConflict("id").merge()
        )
        self.assertIn('MERGE INTO "events" AS target', sql)
        self.assertIn('WHEN MATCHED THEN UPDATE SET "name" = source."name"', sql)
        self.assertIn("WHEN NOT MATCHED THEN INSERT", sql)
        self.assertFalse(sql.endswith(";"))

    def test_upsert_ignore_has_no_matched_clause(self):
        sql = str(
            self.Event.query().insert({"id": 1, "name": "a"}).onConflict("id").ignore()
        )
        self.assertNotIn("WHEN MATCHED", sql)
        self.assertIn("WHEN NOT MATCHED THEN INSERT", sql)

    def test_returning_raises(self):
        with self.assertRaises(DialectError):
            str(self.Event.query().insert({"id": 1}).returning("id"))

    def test_temporary_ctas_raises(self):
        query = self.Event.query().select("id")
        with self.assertRaises(DialectError):
            str(query.create_table_as("t2", temporary=True))

    def test_ctas_renders(self):
        sql = str(self.Event.query().select("id").create_table_as("t2"))
        self.assertTrue(sql.startswith('CREATE TABLE "t2" AS'))


class TestAthenaTypes(unittest.TestCase):
    def test_type_map(self):
        model = athena_model(
            "AthTypes",
            "typed",
            {
                "a": Integer(),
                "b": String(50),
                "c": Text(),
                "d": Boolean(),
                "e": Float(),
                "f": Numeric(10, 2),
                "g": Date(),
                "h": Timestamp(),
                "i": Json(),
            },
        )
        sql = model.create_table_sql()
        self.assertIn('"a" INT', sql)
        self.assertIn('"b" VARCHAR(50)', sql)
        self.assertIn('"c" STRING', sql)
        self.assertIn('"d" BOOLEAN', sql)
        self.assertIn('"e" DOUBLE', sql)
        self.assertIn('"f" DECIMAL(10, 2)', sql)
        self.assertIn('"g" DATE', sql)
        self.assertIn('"h" TIMESTAMP', sql)
        self.assertIn('"i" STRING', sql)

    def test_unbounded_varchar_renders_string(self):
        from sustained.schema import ColumnDef

        compiler = Dialects.get_compiler(Dialects.ATHENA)
        self.assertEqual(compiler.compile_column_type(ColumnDef("VARCHAR")), "STRING")


class TestAthenaConstraints(unittest.TestCase):
    def _assert_rejected(self, column):
        model = athena_model("AthC", "constrained", {"c": column})
        with self.assertRaises(DialectError):
            model.create_table_sql()

    def test_primary_key_rejected(self):
        self._assert_rejected(Integer(primary_key=True))

    def test_unique_rejected(self):
        self._assert_rejected(String(50, unique=True))

    def test_default_rejected(self):
        self._assert_rejected(Boolean(default=True))

    def test_references_rejected(self):
        self._assert_rejected(Integer(references="other.id"))

    def test_not_null_rejected(self):
        self._assert_rejected(String(50, nullable=False))

    def test_autoincrement_rejected(self):
        self._assert_rejected(Integer(primary_key=True, autoincrement=True))

    def test_indexes_rejected(self):
        model = athena_model(
            "AthIx",
            "indexed",
            {"a": Integer()},
            indexes=[Index("ix_a", "a")],
        )
        with self.assertRaises(DialectError):
            model.create_indexes_sql()


class TestAthenaTableOptions(unittest.TestCase):
    def test_full_suffix(self):
        model = athena_model(
            "AthOpts",
            "events",
            {"id": Integer()},
            tableOptions=TableOptions(
                location="s3://bucket/events/",
                partitioned_by=["day(created_at)"],
                properties={"table_type": "ICEBERG"},
            ),
        )
        sql = model.create_table_sql()
        self.assertIn("PARTITIONED BY (day(created_at))", sql)
        self.assertIn("LOCATION 's3://bucket/events/'", sql)
        self.assertIn("TBLPROPERTIES ('table_type'='ICEBERG')", sql)

    def test_options_raise_on_other_dialects(self):
        model = athena_model(
            "AthOptsPg",
            "events",
            {"id": Integer()},
            tableOptions=TableOptions(location="s3://bucket/"),
        )
        model.set_dialect(Dialects.POSTGRES)
        with self.assertRaises(DialectError):
            model.create_table_sql()

    def test_location_quotes_escape(self):
        compiler = Dialects.get_compiler(Dialects.ATHENA)
        suffix = compiler.compile_table_options(TableOptions(location="s3://a'b/"))
        self.assertIn("LOCATION 's3://a''b/'", suffix)


class TestAthenaDdl(unittest.TestCase):
    def setUp(self):
        self.compiler = Dialects.get_compiler(Dialects.ATHENA)

    def test_add_column_uses_add_columns(self):
        sql = self.compiler.compile_add_column('"t"', '"x" STRING')
        self.assertEqual(sql, 'ALTER TABLE "t" ADD COLUMNS ("x" STRING)')

    def test_alter_column_type_uses_change_column(self):
        steps = self.compiler.compile_alter_column_type('"t"', "c", "BIGINT")
        self.assertEqual(steps, ['ALTER TABLE "t" CHANGE COLUMN "c" "c" BIGINT'])

    def test_alter_column_type_rejects_using(self):
        with self.assertRaises(DialectError):
            self.compiler.compile_alter_column_type('"t"', "c", "BIGINT", using="x")

    def test_alter_nullability_raises(self):
        with self.assertRaises(DialectError):
            self.compiler.compile_alter_column_nullability('"t"', "c", "BIGINT", True)

    def test_rename_column_raises(self):
        with self.assertRaises(DialectError):
            self.compiler.compile_rename_column('"t"', "a", "b")

    def test_rename_table_raises(self):
        with self.assertRaises(DialectError):
            self.compiler.compile_rename_table('"a"', '"b"')

    def test_index_ddl_raises(self):
        with self.assertRaises(DialectError):
            self.compiler.compile_create_index("i", '"t"', ["a"], False)
        with self.assertRaises(DialectError):
            self.compiler.compile_drop_index("i", '"t"')

    def test_identity_raises(self):
        with self.assertRaises(DialectError):
            self.compiler.compile_identity()

    def test_capability_flags(self):
        self.assertTrue(self.compiler.supports_alter_column())
        self.assertFalse(self.compiler.supports_constraints())
        self.assertFalse(self.compiler.supports_transactions())


class FakeAthenaCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def execute(self, sql, params=()):
        self._conn.log.append(sql)
        if "information_schema.columns" in sql:
            self._rows = self._conn.columns_rows
        elif "information_schema" in sql:
            self._rows = []
        elif sql.startswith("SELECT") and "checksum" in sql:
            self._rows = [
                (i, n, None, True, False) for n, i in enumerate(self._conn.applied, 1)
            ]
        elif sql.startswith("SELECT") and "sustained_migrations" in sql:
            self._rows = [(i,) for i in self._conn.applied]
        elif sql.startswith("INSERT INTO") and "sustained_migrations" in sql:
            self._conn.applied.append(params[0])
        elif sql.startswith("DELETE FROM") and "sustained_migrations" in sql:
            self._conn.applied.remove(params[0])
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeAthenaConnection:
    """No transactions: commit is a no-op and rollback raises, as pyathena's does."""

    def __init__(self, columns_rows=()):
        self.columns_rows = list(columns_rows)
        self.applied = []
        self.log = []

    def cursor(self):
        return FakeAthenaCursor(self)

    def commit(self):
        self.log.append("<commit>")

    def rollback(self):
        raise RuntimeError("Athena has no transactions")


class TestAthenaMigrator(unittest.TestCase):
    def _migrator(self, conn, migrations):
        return Migrator(
            conn,
            migrations,
            dialect=Dialects.ATHENA,
            tracking_table_options=TableOptions(
                location="s3://bucket/meta/",
                properties={"table_type": "ICEBERG"},
            ),
        )

    def test_tracking_table_has_no_constraints_and_takes_options(self):
        conn = FakeAthenaConnection()
        self._migrator(conn, []).up()
        create = next(s for s in conn.log if s.startswith("CREATE TABLE"))
        self.assertNotIn("PRIMARY KEY", create)
        self.assertNotIn("NOT NULL", create)
        self.assertIn("LOCATION 's3://bucket/meta/'", create)
        self.assertIn("TBLPROPERTIES ('table_type'='ICEBERG')", create)

    def test_receipt_table_has_no_constraints_either(self):
        conn = FakeAthenaConnection()
        self._migrator(conn, []).record_rehearsal("0" * 64)
        create = next(
            s for s in conn.log if s.startswith('CREATE TABLE IF NOT EXISTS "sus')
        )
        self.assertIn("sustained_rehearsals", create)
        self.assertNotIn("PRIMARY KEY", create)
        self.assertNotIn("NOT NULL", create)

    def test_up_and_down_run_without_transactions(self):
        conn = FakeAthenaConnection()
        migrator = self._migrator(
            conn,
            [
                Migration(
                    "one",
                    up="ALTER TABLE t ADD COLUMNS (x STRING)",
                    down="ALTER TABLE t DROP COLUMN x",
                )
            ],
        )
        self.assertEqual(migrator.up(), ["one"])
        self.assertEqual(migrator.down(), ["one"])
        joined = " ".join(conn.log)
        self.assertNotIn("BEGIN", joined)
        self.assertNotIn("SAVEPOINT", joined)

    def test_failing_step_does_not_call_rollback(self):
        conn = FakeAthenaConnection()

        def explode(connection):
            raise ValueError("boom")

        migrator = self._migrator(conn, [Migration("bad", up=explode)])
        # rollback() raises RuntimeError, so seeing the original ValueError
        # proves the migrator never called it.
        with self.assertRaises(ValueError):
            migrator.up()

    def test_sync_diffs_clean_against_information_schema(self):
        from sustained.autogenerate import diff_schema

        model = athena_model(
            "AthSync",
            "ath_events",
            {"id": Integer(), "name": String(120), "body": Text()},
        )
        conn = FakeAthenaConnection(
            [
                ("ath_events", "id", "integer", "YES", None),
                ("ath_events", "name", "varchar(120)", "YES", None),
                ("ath_events", "body", "string", "YES", None),
            ]
        )
        diff = diff_schema(conn, [model], dialect=Dialects.ATHENA)
        self.assertTrue(diff.is_empty())

    def test_sync_generates_add_columns(self):
        model = athena_model(
            "AthSyncAdd",
            "ath_events",
            {"id": Integer(), "note": Text()},
        )
        conn = FakeAthenaConnection([("ath_events", "id", "integer", "YES", None)])
        migrator = self._migrator(conn, [])
        applied = migrator.up(models=[model], migration_id="add_note")
        self.assertEqual(applied, ["add_note"])
        adds = [s for s in conn.log if "ADD COLUMNS" in s]
        self.assertEqual(adds, ['ALTER TABLE "ath_events" ADD COLUMNS ("note" STRING)'])


if __name__ == "__main__":
    unittest.main()
