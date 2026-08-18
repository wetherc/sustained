"""
Tests for ddl migration steps: per-dialect rendering, the derived down
step, refusal to derive one from an irreversible step, dialect-independent
checksums, and a full apply-and-revert cycle on SQLite through the
Migrator, including what the guards and destructive labels read.
"""

import sqlite3
import unittest

from sustained import DialectError, GuardBlocked, Model
from sustained.ddl import (
    DdlStep,
    add_check,
    add_column,
    add_enum_value,
    add_foreign_key,
    create_enum,
    create_index,
    create_table,
    drop_column,
    drop_constraint,
    drop_enum,
    drop_foreign_key,
    drop_index,
    drop_table,
    rename_column,
    rename_table,
    sql,
)
from sustained.dialects import Dialects
from sustained.guards import no_drops
from sustained.migrations import Migration, Migrator, migration_checksum, migration_sql
from sustained.schema import (
    Check,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    TableOptions,
)

ANSI = Dialects.get_compiler(Dialects.DEFAULT)
POSTGRES = Dialects.get_compiler(Dialects.POSTGRES)
MYSQL = Dialects.get_compiler(Dialects.MYSQL)


class Reader(Model):
    tableName = "readers"
    tableColumns = {
        "id": Integer(primary_key=True, autoincrement=True),
        "email": String(120, unique=True, nullable=False),
        "status": Enum("active", "dormant", name="reader_status", default="active"),
    }
    tableConstraints = [Check("ck_readers_email", "email <> ''")]
    indexes = [Index("ix_readers_email", "email")]


class TestRendering(unittest.TestCase):
    def test_create_table_from_model_renders_table_constraints_indexes(self):
        statements = create_table(Reader).render(ANSI)
        self.assertEqual(len(statements), 2)
        self.assertIn("CREATE TABLE readers", statements[0])
        self.assertIn("ck_readers_email", statements[0])
        self.assertIn("ck_readers_status_enum", statements[0])
        self.assertIn("CREATE INDEX ix_readers_email", statements[1])

    def test_create_table_prepends_enum_types_on_native_dialect(self):
        statements = create_table(Reader).render(POSTGRES)
        self.assertTrue(statements[0].startswith('CREATE TYPE "reader_status"'))
        self.assertIn('CREATE TABLE "readers"', statements[1])

    def test_create_table_from_name_needs_columns(self):
        with self.assertRaisesRegex(ValueError, "needs columns"):
            create_table("bare")

    def test_create_table_explicit_columns(self):
        step = create_table("plants", columns={"id": Integer(primary_key=True)})
        self.assertIn("CREATE TABLE plants", step.render(ANSI)[0])

    def test_drop_table_from_model_drops_enum_types_on_native_dialect(self):
        statements = drop_table(Reader).render(POSTGRES)
        self.assertEqual(statements[0], 'DROP TABLE "readers"')
        self.assertEqual(statements[1], 'DROP TYPE "reader_status"')

    def test_drop_table_by_name_drops_only_the_table(self):
        self.assertEqual(
            drop_table("readers").render(POSTGRES), ['DROP TABLE "readers"']
        )

    def test_add_column(self):
        step = add_column("readers", "bio", String(200))
        self.assertEqual(
            step.render(ANSI),
            ["ALTER TABLE readers ADD COLUMN bio VARCHAR(200)"],
        )

    def test_add_enum_column_on_check_dialect_adds_the_constraint(self):
        step = add_column("readers", "tier", Enum("free", "paid", name="reader_tier"))
        statements = step.render(ANSI)
        self.assertEqual(len(statements), 2)
        self.assertIn("ADD COLUMN", statements[0])
        self.assertIn("VARCHAR(4)", statements[0])
        self.assertIn("ck_readers_tier_enum", statements[1])
        self.assertIn("IN ('free', 'paid')", statements[1])

    def test_add_enum_column_on_native_dialect_is_one_statement(self):
        step = add_column("readers", "tier", Enum("free", "paid", name="reader_tier"))
        statements = step.render(POSTGRES)
        self.assertEqual(len(statements), 1)
        self.assertIn('"reader_tier"', statements[0])

    def test_rename_column_and_table(self):
        self.assertEqual(
            rename_column("readers", "email", "address").render(ANSI),
            ["ALTER TABLE readers RENAME COLUMN email TO address"],
        )
        self.assertEqual(
            rename_table("readers", "subscribers").render(ANSI),
            ["ALTER TABLE readers RENAME TO subscribers"],
        )

    def test_add_foreign_key_renders_actions(self):
        fk = ForeignKey(
            "fk_loans_reader", "reader_id", "readers.id", on_delete="CASCADE"
        )
        statements = add_foreign_key("loans", fk).render(ANSI)
        self.assertIn("ADD CONSTRAINT", statements[0])
        self.assertIn("ON DELETE CASCADE", statements[0])

    def test_add_check_and_drops(self):
        statements = add_check("loans", Check("ck_days", "days > 0")).render(ANSI)
        self.assertIn("CHECK (days > 0)", statements[0])
        self.assertIn(
            "DROP CONSTRAINT", drop_constraint("loans", "ck_days").render(ANSI)[0]
        )
        self.assertIn(
            "DROP CONSTRAINT", drop_foreign_key("loans", "fk_x").render(ANSI)[0]
        )

    def test_index_steps(self):
        statements = create_index(
            "readers", Index("ix_status", "status", unique=True)
        ).render(ANSI)
        self.assertIn("CREATE UNIQUE INDEX", statements[0])
        self.assertEqual(
            drop_index("readers", "ix_status").render(ANSI), ["DROP INDEX ix_status"]
        )

    def test_enum_type_steps_on_postgres(self):
        self.assertEqual(
            create_enum("mood", "ok", "sad").render(POSTGRES),
            ["CREATE TYPE \"mood\" AS ENUM ('ok', 'sad')"],
        )
        self.assertEqual(drop_enum("mood").render(POSTGRES), ['DROP TYPE "mood"'])
        self.assertEqual(
            add_enum_value("mood", "great").render(POSTGRES),
            ["ALTER TYPE \"mood\" ADD VALUE 'great'"],
        )

    def test_enum_type_steps_refuse_on_check_dialect(self):
        with self.assertRaises(DialectError):
            create_enum("mood", "ok").render(ANSI)

    def test_sql_escape_hatch(self):
        self.assertEqual(
            sql("UPDATE readers SET status = 'active'").render(MYSQL),
            ["UPDATE readers SET status = 'active'"],
        )

    def test_mysql_rendering_uses_backticks(self):
        statements = add_column("readers", "bio", String(200)).render(MYSQL)
        self.assertIn("`readers`", statements[0])

    def test_dotted_table_names_quote_per_part(self):
        step = drop_column("warehouse.readers", "bio")
        self.assertIn('"warehouse"."readers"', step.render(POSTGRES)[0])

    def test_factories_validate_names(self):
        for build in (
            lambda: add_column("t", "", Integer()),
            lambda: drop_column("t", ""),
            lambda: rename_column("t", "", "b"),
            lambda: rename_table("t", ""),
            lambda: drop_foreign_key("t", ""),
            lambda: drop_constraint("t", ""),
            lambda: drop_index("t", ""),
            lambda: create_enum(""),
            lambda: create_enum("mood"),
            lambda: drop_enum(""),
            lambda: add_enum_value("mood", ""),
            lambda: sql("   "),
            lambda: create_table(""),
        ):
            with self.assertRaises(ValueError):
                build()

    def test_unknown_operation_refused(self):
        with self.assertRaisesRegex(ValueError, "Unknown ddl operation"):
            DdlStep("explode", {})

    def test_repr_names_the_factory(self):
        self.assertIn("ddl.drop_index(", repr(drop_index("t", "ix")))


class TestInverses(unittest.TestCase):
    def test_reversible_steps_invert(self):
        pairs = [
            (create_table(Reader), "drop_table"),
            (add_column("t", "c", Integer()), "drop_column"),
            (rename_column("t", "a", "b"), "rename_column"),
            (rename_table("t", "u"), "rename_table"),
            (add_foreign_key("t", ForeignKey("fk", "a", "u.id")), "drop_foreign_key"),
            (add_check("t", Check("ck", "a > 0")), "drop_constraint"),
            (create_index("t", Index("ix", "a")), "drop_index"),
            (create_enum("mood", "ok"), "drop_enum"),
        ]
        for step, inverse_op in pairs:
            self.assertTrue(step.reversible)
            self.assertEqual(step.inverse().op, inverse_op)

    def test_irreversible_steps_have_no_inverse(self):
        for step in (
            drop_table("t"),
            drop_column("t", "c"),
            drop_foreign_key("t", "fk"),
            drop_constraint("t", "ck"),
            drop_index("t", "ix"),
            drop_enum("mood"),
            add_enum_value("mood", "great"),
            sql("DELETE FROM t"),
        ):
            self.assertFalse(step.reversible)
            self.assertIsNone(step.inverse())

    def test_rename_inverse_swaps_names(self):
        inverse = rename_column("t", "old", "new").inverse()
        self.assertEqual(inverse.args["old"], "new")
        self.assertEqual(inverse.args["new"], "old")

    def test_create_table_inverse_drops_its_enum_types(self):
        statements = create_table(Reader).inverse().render(POSTGRES)
        self.assertEqual(statements[1], 'DROP TYPE "reader_status"')

    def test_enum_add_column_inverse_drops_the_check_first(self):
        inverse = add_column(
            "readers", "tier", Enum("free", "paid", name="reader_tier")
        ).inverse()
        statements = inverse.render(ANSI)
        self.assertIn("ck_readers_tier_enum", statements[0])
        self.assertIn("DROP COLUMN", statements[1])
        self.assertEqual(len(inverse.render(POSTGRES)), 1)


class TestDerivedDown(unittest.TestCase):
    def test_all_reversible_steps_derive_the_down(self):
        migration = Migration(
            "001_shelves",
            up=[
                create_table("shelves", columns={"id": Integer(primary_key=True)}),
                create_index("shelves", Index("ix_shelves_id", "id")),
            ],
        )
        self.assertIsNotNone(migration.down)
        rendered = migration_sql(migration, "down", ANSI)
        self.assertEqual(rendered[0], "DROP INDEX ix_shelves_id")
        self.assertEqual(rendered[1], "DROP TABLE shelves")

    def test_irreversible_step_refuses_derivation(self):
        with self.assertRaisesRegex(ValueError, "drop_column does not reverse"):
            Migration("002", up=[drop_column("shelves", "label")])

    def test_raw_string_in_a_ddl_list_refuses_derivation(self):
        with self.assertRaisesRegex(ValueError, "raw SQL string"):
            Migration(
                "002",
                up=[add_column("t", "c", Integer()), "UPDATE t SET c = 0"],
            )

    def test_explicit_down_none_declares_irreversible(self):
        migration = Migration("002", up=[drop_column("shelves", "label")], down=None)
        self.assertIsNone(migration.down)

    def test_explicit_down_step_wins(self):
        migration = Migration(
            "002",
            up=[drop_column("shelves", "label")],
            down=[add_column("shelves", "label", String(40))],
        )
        self.assertIsNotNone(migration.down)

    def test_plain_string_steps_stay_without_a_down(self):
        migration = Migration("003", up="CREATE TABLE t (id INTEGER)")
        self.assertIsNone(migration.down)

    def test_repeatable_never_derives_a_down(self):
        migration = Migration(
            "seed_shelves",
            up=[create_table("shelves", columns={"id": Integer(primary_key=True)})],
            repeatable=True,
        )
        self.assertIsNone(migration.down)


class TestChecksums(unittest.TestCase):
    def test_checksum_is_dialect_independent(self):
        step = add_column("readers", "bio", String(200))
        self.assertNotEqual(step.render(ANSI), step.render(MYSQL))
        migration = Migration("001", up=[step], down=None)
        self.assertEqual(len({migration_checksum(migration)}), 1)
        self.assertIsNotNone(migration_checksum(migration))

    def test_checksum_changes_with_the_arguments(self):
        one = Migration("001", up=[add_column("t", "a", Integer())], down=None)
        two = Migration("001", up=[add_column("t", "b", Integer())], down=None)
        self.assertNotEqual(migration_checksum(one), migration_checksum(two))

    def test_signature_covers_constraint_objects(self):
        step = add_foreign_key(
            "loans",
            ForeignKey("fk", ("a", "b"), ("u.x", "u.y"), on_update="SET NULL"),
        )
        signature = step.signature()
        self.assertIn('"$foreign_key"', signature)
        self.assertIn('"SET NULL"', signature)

    def test_signature_covers_options_and_defaults(self):
        step = create_table(
            "logs",
            columns={"id": Integer(primary_key=True), "note": String(10)},
            options=TableOptions(location="s3://x", properties={"b": "2", "a": "1"}),
        )
        self.assertIn('"$options"', step.signature())

    def test_unserializable_argument_refused(self):
        step = sql("SELECT 1")
        step.args["bad"] = object()
        with self.assertRaisesRegex(TypeError, "cannot canonicalize"):
            step.signature()


class TestMigratorIntegration(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)

    def _columns(self, table):
        cursor = self.connection.execute(f"PRAGMA table_info({table})")
        return [row[1] for row in cursor.fetchall()]

    def test_up_and_down_render_on_the_migrator_dialect(self):
        migration = Migration(
            "001_shelves",
            up=[
                create_table(
                    "shelves",
                    columns={
                        "id": Integer(primary_key=True),
                        "label": String(40, nullable=False),
                    },
                ),
                add_column("shelves", "room", String(20)),
            ],
        )
        migrator = Migrator(self.connection, [migration], dialect=Dialects.DEFAULT)
        self.assertEqual(migrator.up(), ["001_shelves"])
        self.assertEqual(self._columns("shelves"), ["id", "label", "room"])
        self.assertEqual(migrator.down(), ["001_shelves"])
        self.assertEqual(self._columns("shelves"), [])

    def test_guards_read_the_rendered_sql(self):
        migration = Migration("001_drop", up=[drop_table("ghosts")], down=None)
        migrator = Migrator(
            self.connection,
            [migration],
            dialect=Dialects.DEFAULT,
            guards=[no_drops()],
        )
        with self.assertRaises(GuardBlocked):
            migrator.up(unrehearsed=True)

    def test_script_renders_ddl_steps(self):
        migration = Migration(
            "001_shelves",
            up=[create_table("shelves", columns={"id": Integer(primary_key=True)})],
        )
        migrator = Migrator(self.connection, [migration], dialect=Dialects.DEFAULT)
        script = migrator.script("up")
        self.assertIn("CREATE TABLE shelves", script)

    def test_rehearse_runs_the_derived_down(self):
        migration = Migration(
            "001_shelves",
            up=[
                create_table(
                    "shelves",
                    columns={
                        "id": Integer(primary_key=True),
                        "label": String(40, nullable=False),
                    },
                ),
                add_column("shelves", "room", String(20)),
            ],
        )
        migrator = Migrator(self.connection, [migration], dialect=Dialects.DEFAULT)
        rehearsal = migrator.rehearse()
        self.assertTrue(rehearsal.ok)
        for result in rehearsal:
            self.assertEqual([], result.reversed)
        # The rehearsal rolled everything back.
        self.assertEqual(self._columns("shelves"), [])
        # The derived down still applies for real after the rehearsal.
        self.assertEqual(migrator.up(), ["001_shelves"])
        self.assertEqual(migrator.down(), ["001_shelves"])
        self.assertEqual(self._columns("shelves"), [])

    def test_validate_accepts_a_reapplied_ddl_migration(self):
        def build():
            return Migration(
                "001_shelves",
                up=[create_table("shelves", columns={"id": Integer(primary_key=True)})],
            )

        Migrator(self.connection, [build()], dialect=Dialects.DEFAULT).up()
        migrator = Migrator(self.connection, [build()], dialect=Dialects.DEFAULT)
        self.assertEqual(migrator.validate(raise_on_problems=False), [])


if __name__ == "__main__":
    unittest.main()
