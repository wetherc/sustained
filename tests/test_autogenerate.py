"""
Tests for schema diffing, migration autogeneration, and rollback of
generated migrations, against in-memory SQLite.
"""

import sqlite3
import unittest

from sustained import Model
from sustained import autogenerate as autogenerate_module
from sustained import create_model
from sustained.autogenerate import (
    autogenerate,
    diff_schema,
    diff_snapshots,
    introspect_schema,
    normalize_type,
)
from sustained.dialects import Dialects
from sustained.migrations import Migration, Migrator
from sustained.schema import Boolean, Integer, String, Text


def make_model(name, table, columns):
    model = create_model(name, table)
    model.tableColumns = columns
    model.columns = tuple(columns)
    return model


class AutogenTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.User = make_model(
            f"AgU_{self.id().rsplit('.', 1)[-1]}",
            "ag_users",
            {
                "id": Integer(primary_key=True),
                "email": String(120, nullable=False),
            },
        )

    def tearDown(self):
        self.conn.close()


class TestNormalizeType(unittest.TestCase):
    def test_synonyms_and_parameters(self):
        self.assertEqual(normalize_type("VARCHAR(120)"), "VARCHAR")
        self.assertEqual(normalize_type("character varying"), "VARCHAR")
        self.assertEqual(normalize_type("NVARCHAR(MAX)"), "VARCHAR")
        self.assertEqual(normalize_type("DOUBLE PRECISION"), "FLOAT")
        self.assertEqual(normalize_type("jsonb"), "JSON")
        self.assertEqual(normalize_type("DATETIME2"), "TIMESTAMP")
        self.assertEqual(normalize_type("BIT"), "BOOLEAN")
        self.assertEqual(normalize_type("bytea"), "BINARY")
        self.assertEqual(normalize_type("BLOB"), "BINARY")
        self.assertEqual(normalize_type("longblob"), "BINARY")
        self.assertEqual(normalize_type("VARBINARY(MAX)"), "BINARY")

    def test_unknown_passthrough(self):
        self.assertEqual(normalize_type("GEOMETRY"), "GEOMETRY")


class TestDiffSchema(AutogenTestCase):
    def test_missing_table_detected(self):
        diff = diff_schema(self.conn, [self.User])
        self.assertEqual(diff.missing_tables, [self.User])
        self.assertIn("create table ag_users", diff.summary())

    def test_round_trip_is_empty(self):
        self.User.create_table(self.conn)
        diff = diff_schema(self.conn, [self.User])
        self.assertTrue(diff.is_empty())
        self.assertEqual(diff.summary(), "schema up to date")

    def test_new_column_detected(self):
        self.User.create_table(self.conn)
        self.User.tableColumns["bio"] = Text()
        diff = diff_schema(self.conn, [self.User])
        self.assertEqual(len(diff.new_columns), 1)
        self.assertEqual(diff.new_columns[0][1], "bio")

    def test_extra_objects_detected(self):
        self.User.create_table(self.conn)
        self.conn.execute("ALTER TABLE ag_users ADD COLUMN legacy TEXT")
        self.conn.execute("CREATE TABLE orphan (id INTEGER)")
        diff = diff_schema(self.conn, [self.User])
        self.assertEqual(diff.extra_columns, [("ag_users", "legacy")])
        self.assertEqual(diff.extra_tables, ["orphan"])

    def test_changed_column_detected(self):
        self.User.create_table(self.conn)
        changed = make_model(
            "AgChanged",
            "ag_users",
            {"id": Integer(primary_key=True), "email": Boolean()},
        )
        diff = diff_schema(self.conn, [changed])
        self.assertEqual(len(diff.changed_columns), 1)
        self.assertIn("change column ag_users.email", diff.summary())

    def test_tracking_table_excluded(self):
        self.conn.execute("CREATE TABLE sustained_migrations (id TEXT)")
        self.User.create_table(self.conn)
        diff = diff_schema(self.conn, [self.User])
        self.assertEqual(diff.extra_tables, [])

    def test_duplicate_table_declarations_rejected(self):
        other = make_model("AgDup", "ag_users", {"id": Integer(primary_key=True)})
        with self.assertRaises(ValueError):
            diff_schema(self.conn, [self.User, other])

    def test_model_without_table_columns_rejected(self):
        bare = create_model("AgBare", "bare_tbl")
        with self.assertRaises(ValueError):
            diff_schema(self.conn, [bare])


class TestAutogenerate(AutogenTestCase):
    def test_up_to_date_returns_none(self):
        self.User.create_table(self.conn)
        self.assertIsNone(autogenerate(self.conn, [self.User], id="noop"))

    def test_create_table_migration_is_reversible(self):
        migration = autogenerate(self.conn, [self.User], id="m1")
        self.assertEqual(migration.down, ["DROP TABLE IF EXISTS ag_users"])
        self.assertIn("CREATE TABLE", migration.up[0])

    def test_add_column_migration_is_reversible(self):
        self.User.create_table(self.conn)
        self.User.tableColumns["bio"] = Text()
        migration = autogenerate(self.conn, [self.User], id="m2")
        self.assertEqual(migration.up, ["ALTER TABLE ag_users ADD COLUMN bio TEXT"])
        self.assertEqual(migration.down, ["ALTER TABLE ag_users DROP COLUMN bio"])

    def test_drops_require_opt_in(self):
        self.User.create_table(self.conn)
        self.conn.execute("ALTER TABLE ag_users ADD COLUMN legacy TEXT")
        with self.assertRaises(ValueError):
            autogenerate(self.conn, [self.User], id="m3")
        migration = autogenerate(self.conn, [self.User], id="m3", allow_drops=True)
        self.assertEqual(migration.up, ["ALTER TABLE ag_users DROP COLUMN legacy"])
        self.assertIsNone(migration.down)

    def test_changed_columns_rebuild_on_sqlite(self):
        self.User.create_table(self.conn)
        changed = make_model(
            "AgBlock",
            "ag_users",
            {"id": Integer(primary_key=True), "email": Boolean()},
        )
        migration = autogenerate(self.conn, [changed], id="m4")
        self.assertTrue(any("ag_users_sustained_new" in step for step in migration.up))
        self.assertIsNone(migration.down)
        self.assertIsNone(
            autogenerate(self.conn, [changed], id="m4", ignore_changed_columns=True)
        )

    def test_not_null_add_without_default_rejected(self):
        self.User.create_table(self.conn)
        self.User.tableColumns["req"] = String(10, nullable=False)
        with self.assertRaises(ValueError):
            autogenerate(self.conn, [self.User], id="m5")

    def test_primary_key_add_rejected(self):
        self.User.create_table(self.conn)
        self.User.tableColumns["id2"] = Integer(primary_key=True)
        with self.assertRaises(ValueError):
            autogenerate(self.conn, [self.User], id="m6")


class TestMigratorSync(AutogenTestCase):
    def test_up_with_models_creates_and_is_idempotent(self):
        migrator = Migrator(self.conn, [])
        applied = migrator.up(models=[self.User])
        self.assertEqual(len(applied), 1)
        self.assertTrue(diff_schema(self.conn, [self.User]).is_empty())
        self.assertEqual(migrator.up(models=[self.User]), [])

    def test_up_with_models_then_down_rolls_back(self):
        migrator = Migrator(self.conn, [])
        migrator.up(models=[self.User])
        self.User.tableColumns["bio"] = Text()
        migrator.up(models=[self.User])
        migrator.down()
        columns = introspect_schema(self.conn)["ag_users"]
        self.assertNotIn("bio", columns)

    def test_up_rejects_models_with_a_target(self):
        migrator = Migrator(self.conn, [])
        with self.assertRaises(ValueError) as caught:
            migrator.up(models=[self.User], target="whatever")
        self.assertIn("always runs last", str(caught.exception))

    def test_up_with_models_applies_registered_migrations_too(self):
        migrator = Migrator(
            self.conn,
            [Migration("hand", up="CREATE TABLE hand_written (id INTEGER)")],
        )
        applied = migrator.up(models=[self.User])
        self.assertEqual(applied[0], "hand")
        self.assertEqual(len(applied), 2)

    def test_up_with_models_still_blocks_out_of_order_migrations(self):
        from sustained.exceptions import MigrationError

        Migrator(
            self.conn, [Migration("002_b", up="CREATE TABLE t_b (id INTEGER)")]
        ).up()
        late = Migrator(
            self.conn,
            [
                Migration("001_a", up="CREATE TABLE t_a (id INTEGER)"),
                Migration("002_b", up="CREATE TABLE t_b (id INTEGER)"),
            ],
        )
        with self.assertRaises(MigrationError):
            late.up(models=[self.User])

    def test_sync_still_works_and_warns(self):
        migrator = Migrator(self.conn, [])
        with self.assertWarns(DeprecationWarning) as caught:
            applied = migrator.sync([self.User])
        self.assertEqual(len(applied), 1)
        self.assertIn("up(models=[...])", str(caught.warning))
        self.assertTrue(diff_schema(self.conn, [self.User]).is_empty())

    def test_down_to_target(self):
        migrator = Migrator(
            self.conn,
            [
                Migration("a", up="CREATE TABLE ta (id INTEGER)", down="DROP TABLE ta"),
                Migration("b", up="CREATE TABLE tb (id INTEGER)", down="DROP TABLE tb"),
                Migration("c", up="CREATE TABLE tc (id INTEGER)", down="DROP TABLE tc"),
            ],
        )
        migrator.up()
        reverted = migrator.down_to("a")
        self.assertEqual(reverted, ["c", "b"])
        self.assertEqual(migrator.applied(), ["a"])
        self.assertEqual(migrator.down_to("a"), [])

    def test_down_to_unapplied_target_raises(self):
        migrator = Migrator(self.conn, [])
        with self.assertRaises(ValueError):
            migrator.down_to("nope")


if __name__ == "__main__":
    unittest.main()


class TestIndexDiffing(AutogenTestCase):
    def _indexed_user(self, *indexes):
        from sustained.schema import Index

        model = make_model(
            f"AgIx_{len(indexes)}",
            "ag_users",
            {
                "id": Integer(primary_key=True),
                "email": String(120, nullable=False),
            },
        )
        model.indexes = list(indexes)
        return model

    def test_create_table_includes_indexes(self):
        from sustained.schema import Index

        model = self._indexed_user(Index("ix_email", "email", unique=True))
        migration = autogenerate(self.conn, [model], id="ix1")
        self.assertIn("CREATE UNIQUE INDEX ix_email ON ag_users (email)", migration.up)

    def test_new_index_on_existing_table(self):
        from sustained.schema import Index

        plain = self._indexed_user()
        plain.create_table(self.conn)
        indexed = self._indexed_user(Index("ix_email", "email"))
        migration = autogenerate(self.conn, [indexed], id="ix2")
        self.assertEqual(migration.up, ["CREATE INDEX ix_email ON ag_users (email)"])
        self.assertEqual(migration.down, ["DROP INDEX ix_email"])

    def test_changed_index_rebuilds(self):
        from sustained.schema import Index

        first = self._indexed_user(Index("ix_email", "email"))
        first.create_table(self.conn)
        changed = self._indexed_user(Index("ix_email", "email", unique=True))
        migration = autogenerate(self.conn, [changed], id="ix3")
        self.assertEqual(
            migration.up,
            [
                "DROP INDEX ix_email",
                "CREATE UNIQUE INDEX ix_email ON ag_users (email)",
            ],
        )

    def test_extra_index_requires_opt_in_and_reverses(self):
        plain = self._indexed_user()
        plain.create_table(self.conn)
        self.conn.execute("CREATE INDEX stray_ix ON ag_users (email)")
        with self.assertRaises(ValueError):
            autogenerate(self.conn, [plain], id="ix4")
        migration = autogenerate(self.conn, [plain], id="ix4", allow_drops=True)
        self.assertEqual(migration.up, ["DROP INDEX stray_ix"])
        self.assertEqual(migration.down, ["CREATE INDEX stray_ix ON ag_users (email)"])

    def test_unique_column_backing_index_not_extra(self):
        model = make_model(
            "AgUniqueCol",
            "ag_users",
            {
                "id": Integer(primary_key=True),
                "email": String(120, nullable=False, unique=True),
            },
        )
        model.create_table(self.conn)
        diff = diff_schema(self.conn, [model])
        self.assertTrue(diff.is_empty(), diff.summary())


class TestRenameHints(AutogenTestCase):
    def test_column_rename_generates_reversible_steps(self):
        self.User.create_table(self.conn)
        renamed = make_model(
            "AgRenamed",
            "ag_users",
            {
                "id": Integer(primary_key=True),
                "contact_email": String(120, nullable=False),
            },
        )
        migration = autogenerate(
            self.conn,
            [renamed],
            id="r1",
            renames={"ag_users.email": "contact_email"},
        )
        self.assertEqual(
            migration.up,
            ["ALTER TABLE ag_users RENAME COLUMN email TO contact_email"],
        )
        self.assertEqual(
            migration.down,
            ["ALTER TABLE ag_users RENAME COLUMN contact_email TO email"],
        )

    def test_table_rename(self):
        self.User.create_table(self.conn)
        moved = make_model(
            "AgMoved",
            "ag_people",
            {
                "id": Integer(primary_key=True),
                "email": String(120, nullable=False),
            },
        )
        migration = autogenerate(
            self.conn, [moved], id="r2", table_renames={"ag_users": "ag_people"}
        )
        self.assertEqual(migration.up, ["ALTER TABLE ag_users RENAME TO ag_people"])
        self.assertEqual(migration.down, ["ALTER TABLE ag_people RENAME TO ag_users"])

    def test_unknown_rename_targets_raise(self):
        self.User.create_table(self.conn)
        with self.assertRaises(ValueError):
            diff_schema(self.conn, [self.User], renames={"ag_users.nope": "x"})
        with self.assertRaises(ValueError):
            diff_schema(self.conn, [self.User], table_renames={"nope": "x"})


class TestSqliteRebuild(AutogenTestCase):
    def test_type_change_rebuilds_and_preserves_rows(self):
        self.User.create_table(self.conn)
        self.conn.execute("INSERT INTO ag_users VALUES (1, 'a@x')")
        changed = make_model(
            "AgRebuild",
            "ag_users",
            {"id": Integer(primary_key=True), "email": Text()},
        )
        migration = autogenerate(self.conn, [changed], id="rb1")
        self.assertIsNone(migration.down)
        # A SQLite rebuild drops the old table, so the run needs
        # either a rehearsal row or the recorded override.
        Migrator(self.conn, [migration]).up(unrehearsed=True)
        rows = self.conn.execute("SELECT id, email FROM ag_users").fetchall()
        self.assertEqual(rows, [(1, "a@x")])
        # The override the run recorded lives in the rehearsal table, which
        # the models do not declare.
        self.assertTrue(
            diff_schema(
                self.conn,
                [changed],
                exclude_tables=("sustained_migrations", "sustained_rehearsals"),
            ).is_empty()
        )

    def test_rebuild_preserves_undeclared_columns_and_indexes(self):
        self.User.create_table(self.conn)
        self.conn.execute("ALTER TABLE ag_users ADD COLUMN notes TEXT")
        self.conn.execute("CREATE INDEX ag_users_notes ON ag_users (notes)")
        self.conn.execute("INSERT INTO ag_users VALUES (1, 'a@x', 'keep me')")
        changed = make_model(
            "AgKeep",
            "ag_users",
            {"id": Integer(primary_key=True), "email": Text()},
        )
        migration = autogenerate(self.conn, [changed], id="rb3", ignore_undeclared=True)
        Migrator(self.conn, [migration]).up(unrehearsed=True)
        rows = self.conn.execute("SELECT id, email, notes FROM ag_users").fetchall()
        self.assertEqual(rows, [(1, "a@x", "keep me")])
        names = {row[1] for row in self.conn.execute("PRAGMA index_list(ag_users)")}
        self.assertIn("ag_users_notes", names)

    def test_rebuild_with_allow_drops_drops_undeclared_columns(self):
        self.User.create_table(self.conn)
        self.conn.execute("ALTER TABLE ag_users ADD COLUMN notes TEXT")
        changed = make_model(
            "AgDropCol",
            "ag_users",
            {"id": Integer(primary_key=True), "email": Text()},
        )
        migration = autogenerate(self.conn, [changed], id="rb4", allow_drops=True)
        Migrator(self.conn, [migration]).up(unrehearsed=True)
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(ag_users)")]
        self.assertEqual(columns, ["id", "email"])

    def test_expression_index_does_not_crash_introspection(self):
        self.User.create_table(self.conn)
        self.conn.execute("CREATE INDEX ag_users_lower ON ag_users (lower(email))")
        schema = introspect_schema(self.conn)
        self.assertNotIn("ag_users_lower", schema["ag_users"].indexes)

    def test_not_null_add_with_backfill_rebuilds(self):
        self.User.create_table(self.conn)
        self.conn.execute("INSERT INTO ag_users VALUES (1, 'a@x')")
        self.User.tableColumns["status"] = String(10, nullable=False, backfill="new")
        migration = autogenerate(self.conn, [self.User], id="rb2")
        # A SQLite rebuild drops the old table, so the run needs
        # either a rehearsal row or the recorded override.
        Migrator(self.conn, [migration]).up(unrehearsed=True)
        rows = self.conn.execute("SELECT status FROM ag_users").fetchall()
        self.assertEqual(rows, [("new",)])
        notnull = self.conn.execute("PRAGMA table_info(ag_users)").fetchall()[2][3]
        self.assertEqual(notnull, 1)


class TestConstraintNotes(AutogenTestCase):
    def test_default_mismatch_noted(self):
        self.User.create_table(self.conn)
        drifted = make_model(
            "AgDefault",
            "ag_users",
            {
                "id": Integer(primary_key=True),
                "email": String(120, nullable=False, default="none@x"),
            },
        )
        diff = diff_schema(self.conn, [drifted])
        self.assertTrue(any("default" in n for n in diff.constraint_notes))

    def test_missing_foreign_key_noted(self):
        self.User.create_table(self.conn)
        self.conn.execute("CREATE TABLE ag_teams (id INTEGER PRIMARY KEY)")
        fk_model = make_model(
            "AgFk",
            "ag_users",
            {
                "id": Integer(primary_key=True),
                "email": String(120, nullable=False),
                "team_id": Integer(references="ag_teams.id"),
            },
        )
        self.conn.execute("ALTER TABLE ag_users ADD COLUMN team_id INTEGER")
        diff = diff_schema(
            self.conn,
            [fk_model],
            exclude_tables=("sustained_migrations", "ag_teams"),
        )
        self.assertTrue(any("foreign key" in n for n in diff.constraint_notes))

    def test_notes_do_not_block_generation(self):
        self.User.create_table(self.conn)
        drifted = make_model(
            "AgNoteOnly",
            "ag_users",
            {
                "id": Integer(primary_key=True),
                "email": String(120, nullable=False, default="none@x"),
            },
        )
        self.assertIsNone(autogenerate(self.conn, [drifted], id="n1"))


class TestLengthChanges(AutogenTestCase):
    def test_length_change_detected(self):
        self.User.create_table(self.conn)
        widened = make_model(
            "AgWide",
            "ag_users",
            {
                "id": Integer(primary_key=True),
                "email": String(255, nullable=False),
            },
        )
        diff = diff_schema(self.conn, [widened])
        self.assertEqual(len(diff.changed_columns), 1)


class TestOfflineScript(AutogenTestCase):
    def test_script_renders_pending_sql(self):
        migrator = Migrator(
            self.conn,
            [Migration("m1", up="CREATE TABLE s1 (id INTEGER)", down="DROP TABLE s1")],
        )
        script = migrator.script("up")
        self.assertIn("CREATE TABLE s1 (id INTEGER);", script)
        self.assertIn("INSERT INTO sustained_migrations", script)
        self.assertEqual(migrator.pending()[0].id, "m1")

    def test_script_down_after_apply(self):
        migrator = Migrator(
            self.conn,
            [Migration("m1", up="CREATE TABLE s1 (id INTEGER)", down="DROP TABLE s1")],
        )
        migrator.up()
        script = migrator.script("down")
        self.assertIn("DROP TABLE s1;", script)
        self.assertIn("DELETE FROM sustained_migrations", script)

    def test_script_callable_step_renders_comment(self):
        from sustained.migrations import migration_sql

        rendered = migration_sql(Migration("cb", up=lambda conn: None), "up")
        self.assertIn("callable step", rendered[0])


class FakeCursor:
    """Serves canned information_schema rows for Postgres-dialect tests."""

    def __init__(self, columns_rows):
        self._columns_rows = columns_rows
        self._current = []

    def execute(self, sql, params=()):
        if "information_schema.columns" in sql:
            self._current = self._columns_rows
        else:
            self._current = []

    def fetchall(self):
        return self._current

    def close(self):
        pass


class FakeConnection:
    def __init__(self, columns_rows):
        self._cursor = FakeCursor(columns_rows)

    def cursor(self):
        return self._cursor


class TestAlterGeneration(unittest.TestCase):
    def _pg_model(self, email_def):
        model = make_model(
            "AgPg",
            "pg_users",
            {"id": Integer(primary_key=True), "email": email_def},
        )
        model.set_dialect(Dialects.POSTGRES)
        return model

    def _connection(self, email_type, email_nullable):
        return FakeConnection(
            [
                ("pg_users", "id", "integer", "int4", None, None, None, "NO", None),
                (
                    "pg_users",
                    "email",
                    email_type,
                    email_type,
                    None,
                    None,
                    None,
                    email_nullable,
                    None,
                ),
            ]
        )

    def test_type_change_generates_alter_with_cast(self):
        model = self._pg_model(Integer())
        conn = self._connection("character varying", "YES")
        migration = autogenerate(
            self.conn if False else conn,
            [model],
            id="alter1",
            dialect=Dialects.POSTGRES,
            type_casts={"pg_users.email": "email::integer"},
        )
        self.assertEqual(
            migration.up,
            [
                'ALTER TABLE "pg_users" ALTER COLUMN "email" TYPE INTEGER '
                "USING email::integer"
            ],
        )
        self.assertEqual(
            migration.down,
            ['ALTER TABLE "pg_users" ALTER COLUMN "email" TYPE character varying'],
        )

    def test_tightening_nullability_needs_backfill(self):
        model = self._pg_model(String(120, nullable=False))
        conn = self._connection("character varying", "YES")
        with self.assertRaises(ValueError):
            autogenerate(conn, [model], id="alter2", dialect=Dialects.POSTGRES)

    def test_tightening_with_backfill_emits_update_then_set(self):
        model = self._pg_model(String(120, nullable=False, backfill="none@x"))
        conn = self._connection("character varying", "YES")
        migration = autogenerate(conn, [model], id="alter3", dialect=Dialects.POSTGRES)
        self.assertEqual(
            migration.up,
            [
                'UPDATE "pg_users" SET "email" = \'none@x\' ' 'WHERE "email" IS NULL',
                'ALTER TABLE "pg_users" ALTER COLUMN "email" SET NOT NULL',
            ],
        )
        self.assertEqual(
            migration.down,
            ['ALTER TABLE "pg_users" ALTER COLUMN "email" DROP NOT NULL'],
        )

    def test_not_null_add_with_backfill_three_step(self):
        model = make_model(
            "AgPgAdd",
            "pg_users",
            {
                "id": Integer(primary_key=True),
                "email": String(120),
                "status": String(10, nullable=False, backfill="new"),
            },
        )
        model.set_dialect(Dialects.POSTGRES)
        conn = self._connection("character varying", "YES")
        migration = autogenerate(conn, [model], id="alter4", dialect=Dialects.POSTGRES)
        self.assertEqual(len(migration.up), 3)
        self.assertIn("ADD COLUMN", migration.up[0])
        self.assertNotIn("NOT NULL", migration.up[0])
        self.assertIn("UPDATE", migration.up[1])
        self.assertIn("SET NOT NULL", migration.up[2])
        self.assertEqual(
            migration.down,
            ['ALTER TABLE "pg_users" DROP COLUMN "status"'],
        )


class DiffSnapshotsTestCase(unittest.TestCase):
    """
    diff_snapshots() compares two introspected schemas, which is how a
    rehearsal checks that the down steps put the schema back.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE snap_users (id INTEGER PRIMARY KEY, email TEXT)"
        )

    def tearDown(self):
        self.conn.close()

    def snapshot(self):
        return introspect_schema(self.conn)

    def test_unchanged_schema_reports_nothing(self):
        before = self.snapshot()
        self.assertEqual(diff_snapshots(before, self.snapshot()), [])

    def test_left_behind_table(self):
        before = self.snapshot()
        self.conn.execute("CREATE TABLE snap_audit (id INTEGER)")
        self.assertEqual(
            diff_snapshots(before, self.snapshot()),
            ["table 'snap_audit' left behind"],
        )

    def test_missing_table(self):
        before = self.snapshot()
        self.conn.execute("DROP TABLE snap_users")
        self.assertEqual(
            diff_snapshots(before, self.snapshot()),
            ["table 'snap_users' missing"],
        )

    def test_left_behind_column(self):
        before = self.snapshot()
        self.conn.execute("ALTER TABLE snap_users ADD COLUMN bio TEXT")
        self.assertEqual(
            diff_snapshots(before, self.snapshot()),
            ["column 'snap_users.bio' left behind"],
        )

    def test_missing_column(self):
        self.conn.execute("ALTER TABLE snap_users ADD COLUMN bio TEXT")
        before = self.snapshot()
        self.conn.execute("ALTER TABLE snap_users DROP COLUMN bio")
        self.assertEqual(
            diff_snapshots(before, self.snapshot()),
            ["column 'snap_users.bio' missing"],
        )

    def test_changed_column_names_both_shapes(self):
        before = self.snapshot()
        self.conn.execute("DROP TABLE snap_users")
        self.conn.execute(
            "CREATE TABLE snap_users (id INTEGER PRIMARY KEY, "
            "email VARCHAR(120) NOT NULL)"
        )
        self.assertEqual(
            diff_snapshots(before, self.snapshot()),
            ["column 'snap_users.email' changed: TEXT became " "VARCHAR(120) NOT NULL"],
        )

    def test_type_parameters_alone_count_as_a_change(self):
        self.conn.execute("CREATE TABLE snap_tags (name VARCHAR(10))")
        before = self.snapshot()
        self.conn.execute("DROP TABLE snap_tags")
        self.conn.execute("CREATE TABLE snap_tags (name VARCHAR(20))")
        self.assertEqual(
            diff_snapshots(before, self.snapshot()),
            ["column 'snap_tags.name' changed: VARCHAR(10) became VARCHAR(20)"],
        )

    def test_type_synonyms_are_not_a_change(self):
        self.conn.execute("CREATE TABLE snap_counts (total INT)")
        before = self.snapshot()
        self.conn.execute("DROP TABLE snap_counts")
        self.conn.execute("CREATE TABLE snap_counts (total INTEGER)")
        self.assertEqual(diff_snapshots(before, self.snapshot()), [])

    def test_defaults_and_indexes_are_out_of_scope(self):
        self.conn.execute("CREATE TABLE snap_flags (id INTEGER, on_ INTEGER DEFAULT 0)")
        before = self.snapshot()
        self.conn.execute("CREATE INDEX snap_users_email ON snap_users (email)")
        self.conn.execute("DROP TABLE snap_flags")
        self.conn.execute("CREATE TABLE snap_flags (id INTEGER, on_ INTEGER DEFAULT 1)")
        self.assertEqual(diff_snapshots(before, self.snapshot()), [])


class IntrospectionPlanTestCase(unittest.TestCase):
    """
    The information_schema read degrades to column-only data when the
    constraint views are missing, on both the blocking and async drivers.
    """

    class Cursor:
        def __init__(self, conn):
            self.conn = conn
            self.rows = []

        def execute(self, sql, params=()):
            self.conn.log.append(sql)
            if "table_constraints" in sql:
                raise RuntimeError("no constraint views here")
            self.rows = [("shows", "id", "integer", "NO", None)]

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    class Connection:
        def __init__(self):
            self.log = []

        def cursor(self):
            return IntrospectionPlanTestCase.Cursor(self)

    def test_blocking_driver_degrades_to_columns(self):
        schema = introspect_schema(self.Connection(), Dialects.MSSQL)
        self.assertEqual(list(schema), ["shows"])
        self.assertEqual(schema["shows"].primary_key, ())
        self.assertFalse(schema["shows"].columns["id"].nullable)


class TestSchemaReadOnce(AutogenTestCase):
    """
    autogenerate() needs the live schema twice: once to diff and once to
    build the steps. It reads it once and hands the same snapshot to
    diff_schema(), so a run costs one pragma walk per table, not two.
    """

    def setUp(self):
        super().setUp()
        self.Note = make_model(
            f"AgN_{self.id().rsplit('.', 1)[-1]}",
            "ag_notes",
            {"id": Integer(primary_key=True), "body": Text()},
        )
        self.reads = []
        self.real = autogenerate_module.introspect_schema

        def counted(connection, dialect):
            snapshot = self.real(connection, dialect)
            self.reads.append(dialect)
            return snapshot

        autogenerate_module.introspect_schema = counted
        self.addCleanup(setattr, autogenerate_module, "introspect_schema", self.real)

    def test_autogenerate_reads_the_schema_once(self):
        self.conn.execute("CREATE TABLE ag_notes (id INTEGER PRIMARY KEY)")
        migration = autogenerate(self.conn, [self.Note], id="m")
        self.assertIsNotNone(migration)
        self.assertEqual(len(self.reads), 1)

    def test_a_passed_snapshot_is_not_read_again(self):
        self.conn.execute("CREATE TABLE ag_notes (id INTEGER PRIMARY KEY)")
        snapshot = self.real(self.conn, Dialects.DEFAULT)
        self.reads.clear()
        diff = diff_schema(None, [self.Note], snapshot=snapshot)
        self.assertEqual(self.reads, [])
        self.assertEqual([name for _, name, _ in diff.new_columns], ["body"])

    def test_renames_are_applied_to_the_passed_snapshot(self):
        self.conn.execute("CREATE TABLE ag_notes (id INTEGER PRIMARY KEY, text TEXT)")
        snapshot = self.real(self.conn, Dialects.DEFAULT)
        diff = diff_schema(
            None,
            [self.Note],
            renames={"ag_notes.text": "body"},
            snapshot=snapshot,
        )
        self.assertTrue(diff.is_empty())
        self.assertIn("body", snapshot["ag_notes"].columns)


class TestMissingTableOrder(unittest.TestCase):
    """
    SQLite cannot add a constraint to a table that already exists, so
    every foreign key stays inside CREATE TABLE and the tables have to
    be created in dependency order.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def models(self):
        show = make_model("OrdShow", "ord_shows", {"id": Integer(primary_key=True)})
        ticket = make_model(
            "OrdTicket",
            "ord_tickets",
            {
                "id": Integer(primary_key=True),
                "show_id": Integer(references="ord_shows.id"),
            },
        )
        return [ticket, show]

    def test_a_referenced_table_is_created_first(self):
        diff = diff_schema(self.conn, self.models())
        self.assertEqual(
            [m.tableName for m in diff.missing_tables], ["ord_shows", "ord_tickets"]
        )
        self.assertEqual(diff.constraint_notes, [])

    def test_the_generated_plan_runs(self):
        migration = autogenerate(self.conn, self.models(), id="create")
        for statement in migration.up:
            self.conn.execute(statement)
        self.assertTrue(diff_schema(self.conn, self.models()).is_empty())
        self.assertEqual(
            migration.down,
            ["DROP TABLE IF EXISTS ord_tickets", "DROP TABLE IF EXISTS ord_shows"],
        )

    def test_a_cycle_is_reported_as_a_note(self):
        left = make_model(
            "CycLeft",
            "cyc_left",
            {
                "id": Integer(primary_key=True),
                "right_id": Integer(references="cyc_right.id"),
            },
        )
        right = make_model(
            "CycRight",
            "cyc_right",
            {
                "id": Integer(primary_key=True),
                "left_id": Integer(references="cyc_left.id"),
            },
        )
        diff = diff_schema(self.conn, [left, right])
        self.assertEqual(len(diff.constraint_notes), 1)
        self.assertIn("cycle", diff.constraint_notes[0])
        self.assertIn("cyc_left -> cyc_right -> cyc_left", diff.constraint_notes[0])
        self.assertEqual(
            [m.tableName for m in diff.missing_tables], ["cyc_right", "cyc_left"]
        )

    def test_a_self_reference_needs_no_order(self):
        node = make_model(
            "SelfNode",
            "self_nodes",
            {
                "id": Integer(primary_key=True),
                "parent_id": Integer(references="self_nodes.id"),
            },
        )
        diff = diff_schema(self.conn, [node])
        self.assertEqual(diff.constraint_notes, [])
        self.assertEqual([m.tableName for m in diff.missing_tables], ["self_nodes"])


class PlanCursor:
    """Serves canned information_schema rows, and nothing else."""

    def __init__(self, columns):
        self.columns = columns
        self._current = []

    def execute(self, sql, params=()):
        self._current = self.columns if "information_schema.columns" in sql else []

    def fetchall(self):
        return self._current

    def close(self):
        pass


class PlanConnection:
    def __init__(self, columns):
        self._cursor = PlanCursor(columns)

    def cursor(self):
        return self._cursor


class TestRebuildStrategy(unittest.TestCase):
    """
    A table rebuild is SQLite's answer to a column change it cannot make
    in place. Presto has neither the ALTER nor the statements the rebuild
    needs, so it refuses instead of emitting a plan Trino cannot run.
    """

    def test_each_dialect_reports_its_strategy(self):
        for dialect, expected in (
            (Dialects.DEFAULT, "rebuild"),
            (Dialects.POSTGRES, "alter"),
            (Dialects.MYSQL, "alter"),
            (Dialects.PRESTO, "unsupported"),
            (Dialects.ATHENA, "alter"),
        ):
            with self.subTest(dialect=dialect):
                compiler = Dialects.get_compiler(dialect)
                self.assertEqual(compiler.rebuild_strategy(), expected)

    def presto_model(self):
        model = make_model(
            "PrestoRebuild",
            "pr_events",
            {"id": Integer(), "amount": Integer()},
        )
        model.set_dialect(Dialects.PRESTO)
        return model

    def test_a_column_change_refuses_on_presto(self):
        from sustained.exceptions import DialectError

        conn = PlanConnection(
            [
                ("pr_events", "id", "integer", "YES", None, None),
                ("pr_events", "amount", "varchar", "YES", None, None),
            ]
        )
        with self.assertRaises(DialectError) as caught:
            autogenerate(conn, [self.presto_model()], id="m1", dialect=Dialects.PRESTO)
        self.assertIn("write the migration by hand", str(caught.exception).lower())

    def test_a_not_null_column_refuses_on_presto(self):
        from sustained.exceptions import DialectError

        model = make_model(
            "PrestoNotNull",
            "pr_events",
            {"id": Integer(), "amount": Integer(nullable=False, backfill=0)},
        )
        model.set_dialect(Dialects.PRESTO)
        conn = PlanConnection([("pr_events", "id", "integer", "YES", None, None)])
        with self.assertRaises(DialectError):
            autogenerate(conn, [model], id="m2", dialect=Dialects.PRESTO)


if __name__ == "__main__":
    unittest.main()
