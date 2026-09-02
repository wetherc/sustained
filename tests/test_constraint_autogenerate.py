"""
Tests for diffing and migrating declared table constraints: promotion of
tableConstraints into generated migrations, the SQLite rebuild routing,
and the read flags that keep a degraded catalog read from faking absence.
"""

import sqlite3
import unittest

from sustained.autogenerate import autogenerate, diff_schema
from sustained.dialects import Dialects
from sustained.introspect import (
    IntrospectedForeignKey,
    introspect_schema,
    normalize_check,
)
from sustained.migrations import Migration, Migrator
from sustained.schema import Check, Enum, ForeignKey, Integer, String
from tests.test_autogenerate import make_model


class TestNormalizeCheck(unittest.TestCase):
    def test_whitespace_case_and_parens(self):
        self.assertEqual(
            normalize_check("( price  >   0 )"),
            normalize_check("price > 0"),
        )
        self.assertEqual(
            normalize_check("((status IN ('a', 'b')))"),
            normalize_check("status IN ('a', 'b')"),
        )

    def test_unbalanced_outer_parens_kept(self):
        # '(a) OR (b)' starts and ends with parens that are not one pair.
        self.assertEqual(
            normalize_check("(price > 0) OR (price < 9)"), "(price > 0) or (price < 9)"
        )

    def test_literal_case_folds_together(self):
        # Casefolding may merge literals differing only by case; the safe
        # direction, since a false match never generates a drop.
        self.assertEqual(normalize_check("s = 'A'"), normalize_check("s = 'a'"))


class SqliteConstraintTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def _run(self, migration):
        migrator = Migrator(self.conn, [migration])
        # A rebuild carries a DROP TABLE, which the receipt gate stops
        # without a rehearsal; these tests exercise generation, not the gate.
        migrator.up(unrehearsed=True)


class TestSqliteCheckPromotion(SqliteConstraintTestCase):
    def _model(self, constraints):
        model = make_model(
            f"CkM_{self.id().rsplit('.', 1)[-1]}",
            "ck_items",
            {"id": Integer(primary_key=True), "price": Integer()},
        )
        model.tableConstraints = constraints
        return model

    def test_missing_check_rebuilds_and_enforces(self):
        self.conn.execute("CREATE TABLE ck_items (id INTEGER PRIMARY KEY, price INT)")
        model = self._model([Check("ck_items_price", "price > 0")])
        migration = autogenerate(self.conn, [model], id="m1")
        self.assertIsNotNone(migration)
        self.assertIsNone(migration.down)
        self.assertTrue(any("ck_items_sustained_new" in s for s in migration.up))
        self._run(migration)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO ck_items (id, price) VALUES (1, -5)")
        self.conn.execute("INSERT INTO ck_items (id, price) VALUES (1, 5)")
        # The schema now matches: no further migration is generated.
        self.assertIsNone(
            autogenerate(self.conn, [model], id="m2", ignore_undeclared=True)
        )

    def test_changed_check_rebuilds_with_new_expression(self):
        self.conn.execute(
            "CREATE TABLE ck_items (id INTEGER PRIMARY KEY, price INT, "
            'CONSTRAINT "ck_items_price" CHECK (price > 0))'
        )
        model = self._model([Check("ck_items_price", "price > 10")])
        migration = autogenerate(self.conn, [model], id="m1")
        self.assertIsNotNone(migration)
        self._run(migration)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO ck_items (id, price) VALUES (1, 5)")

    def test_rows_survive_the_rebuild(self):
        self.conn.execute("CREATE TABLE ck_items (id INTEGER PRIMARY KEY, price INT)")
        self.conn.execute("INSERT INTO ck_items (id, price) VALUES (1, 7)")
        model = self._model([Check("ck_items_price", "price > 0")])
        self._run(autogenerate(self.conn, [model], id="m1"))
        rows = self.conn.execute("SELECT id, price FROM ck_items").fetchall()
        self.assertEqual(rows, [(1, 7)])


class TestSqliteForeignKeyPromotion(SqliteConstraintTestCase):
    def _models(self, constraints):
        owner = make_model(
            f"FkO_{self.id().rsplit('.', 1)[-1]}",
            "fk_owners",
            {"id": Integer(primary_key=True)},
        )
        pet = make_model(
            f"FkP_{self.id().rsplit('.', 1)[-1]}",
            "fk_pets",
            {"id": Integer(primary_key=True), "owner_id": Integer()},
        )
        pet.tableConstraints = constraints
        return owner, pet

    def test_missing_foreign_key_rebuilds(self):
        self.conn.execute("CREATE TABLE fk_owners (id INTEGER PRIMARY KEY)")
        self.conn.execute("CREATE TABLE fk_pets (id INTEGER PRIMARY KEY, owner_id INT)")
        owner, pet = self._models(
            [
                ForeignKey(
                    "fk_pets_owner",
                    "owner_id",
                    "fk_owners.id",
                    on_delete="CASCADE",
                )
            ]
        )
        migration = autogenerate(self.conn, [owner, pet], id="m1")
        self.assertIsNotNone(migration)
        self._run(migration)
        snapshot = introspect_schema(self.conn)
        fk = snapshot["fk_pets"].foreign_keys["fk_pets_owner"]
        self.assertEqual(fk.columns, ("owner_id",))
        self.assertEqual(fk.target_table, "fk_owners")
        self.assertEqual(fk.on_delete, "CASCADE")
        self.assertIsNone(
            autogenerate(self.conn, [owner, pet], id="m2", ignore_undeclared=True)
        )

    def test_shorthand_references_is_not_an_extra(self):
        owner = make_model("FkO2", "fk_owners", {"id": Integer(primary_key=True)})
        pet = make_model(
            "FkP2",
            "fk_pets",
            {
                "id": Integer(primary_key=True),
                "owner_id": Integer(references="fk_owners.id"),
            },
        )
        Migrator(self.conn, []).up(models=[owner, pet])
        diff = diff_schema(self.conn, [owner, pet])
        self.assertEqual(diff.extra_foreign_keys, [])
        self.assertIsNone(autogenerate(self.conn, [owner, pet], id="m1"))


class TestSqliteRebuildCarriesConstraints(SqliteConstraintTestCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("CREATE TABLE fk_owners (id INTEGER PRIMARY KEY)")
        self.conn.execute(
            "CREATE TABLE carry_items (id INTEGER PRIMARY KEY, price INT, "
            "owner_id INT, "
            'CONSTRAINT "ck_carry" CHECK (price > 0), '
            'CONSTRAINT "fk_carry_owner" FOREIGN KEY (owner_id) '
            "REFERENCES fk_owners (id) ON DELETE CASCADE)"
        )
        # A column change forces the rebuild.
        self.model = make_model(
            f"Cr_{self.id().rsplit('.', 1)[-1]}",
            "carry_items",
            {
                "id": Integer(primary_key=True),
                "price": String(20),
                "owner_id": Integer(),
            },
        )
        self.owner = make_model(
            f"CrO_{self.id().rsplit('.', 1)[-1]}",
            "fk_owners",
            {"id": Integer(primary_key=True)},
        )

    def test_undeclared_constraints_survive_without_allow_drops(self):
        migration = autogenerate(
            self.conn, [self.owner, self.model], id="m1", ignore_undeclared=True
        )
        self._run(migration)
        snapshot = introspect_schema(self.conn)
        table = snapshot["carry_items"]
        self.assertIn("ck_carry", table.checks)
        self.assertIn("fk_carry_owner", table.foreign_keys)
        self.assertEqual(table.foreign_keys["fk_carry_owner"].on_delete, "CASCADE")

    def test_allow_drops_drops_undeclared_constraints(self):
        migration = autogenerate(
            self.conn, [self.owner, self.model], id="m1", allow_drops=True
        )
        self._run(migration)
        snapshot = introspect_schema(self.conn)
        table = snapshot["carry_items"]
        self.assertNotIn("ck_carry", table.checks)
        self.assertNotIn("fk_carry_owner", table.foreign_keys)

    def test_undeclared_constraints_refuse_by_default(self):
        with self.assertRaises(ValueError) as caught:
            autogenerate(self.conn, [self.owner, self.model], id="m1")
        self.assertIn("ck_carry", str(caught.exception))
        self.assertIn("fk_carry_owner", str(caught.exception))


class TestSqliteEnumCheckNotExtra(SqliteConstraintTestCase):
    def test_enum_check_is_implied_by_the_column(self):
        model = make_model(
            "EnI",
            "en_items",
            {
                "id": Integer(primary_key=True),
                "status": Enum("draft", "live", name="en_status"),
            },
        )
        Migrator(self.conn, []).up(models=[model])
        diff = diff_schema(self.conn, [model])
        self.assertEqual(diff.extra_checks, [])
        self.assertEqual(diff.new_checks, [])
        self.assertIsNone(autogenerate(self.conn, [model], id="m1"))


class TestChecksWithoutTheCkPrefix(SqliteConstraintTestCase):
    """
    Declared checks carry whatever name the model gives them. The reader
    has to hand those names back, or every run diffs the same check as
    new and rebuilds the table again.
    """

    def _model(self, constraints):
        model = make_model(
            f"Np_{self.id().rsplit('.', 1)[-1]}",
            "np_items",
            {"id": Integer(primary_key=True), "price": Integer()},
        )
        model.tableConstraints = constraints
        return model

    def test_a_declared_check_settles_after_one_migration(self):
        self.conn.execute("CREATE TABLE np_items (id INTEGER PRIMARY KEY, price INT)")
        model = self._model([Check("price_positive", "price > 0")])
        self._run(autogenerate(self.conn, [model], id="m1"))
        self.assertIsNone(
            autogenerate(self.conn, [model], id="m2", ignore_undeclared=True)
        )

    def test_an_undeclared_named_check_survives_a_rebuild(self):
        self.conn.execute(
            "CREATE TABLE np_items (id INTEGER PRIMARY KEY, price INT, "
            "CONSTRAINT price_positive CHECK (price > 0))"
        )
        model = self._model([])
        model.tableColumns["note"] = String(40, nullable=False, backfill="")
        migration = autogenerate(self.conn, [model], id="m1", ignore_undeclared=True)
        self._run(migration)
        snapshot = introspect_schema(self.conn)
        self.assertIn("price_positive", snapshot["np_items"].checks)

    def test_an_undeclared_named_check_refuses_by_default(self):
        self.conn.execute(
            "CREATE TABLE np_items (id INTEGER PRIMARY KEY, price INT, "
            "CONSTRAINT price_positive CHECK (price > 0))"
        )
        model = self._model([])
        model.tableColumns["note"] = String(40, nullable=False, backfill="")
        with self.assertRaises(ValueError) as caught:
            autogenerate(self.conn, [model], id="m1")
        self.assertIn("price_positive", str(caught.exception))


class TestRenameRewritesChecks(SqliteConstraintTestCase):
    def test_a_renamed_column_is_rewritten_inside_its_check(self):
        self.conn.execute(
            "CREATE TABLE rn_items (id INTEGER PRIMARY KEY, price INT, "
            "CONSTRAINT amount_positive CHECK (price > 0))"
        )
        model = make_model(
            "RnItems",
            "rn_items",
            {"id": Integer(primary_key=True), "cost": Integer()},
        )
        model.tableConstraints = [Check("amount_positive", "cost > 0")]
        migration = autogenerate(
            self.conn, [model], id="m1", renames={"rn_items.price": "cost"}
        )
        # The rename is the whole difference: the check matches once its
        # column is compared under the new name, so no rebuild runs.
        self.assertTrue(any("RENAME COLUMN" in s for s in migration.up))
        self.assertFalse(any("rn_items_sustained_new" in s for s in migration.up))
        self._run(migration)
        self.assertIsNone(
            autogenerate(self.conn, [model], id="m2", ignore_undeclared=True)
        )

    def test_rewriting_skips_string_literals_and_handles_quotes(self):
        from sustained.autogenerate import _rename_in_expression

        self.assertEqual(
            _rename_in_expression("price > 0 AND note <> 'price'", "price", "cost"),
            "cost > 0 AND note <> 'price'",
        )
        self.assertEqual(
            _rename_in_expression('"price" > 0', "price", "cost"),
            '"cost" > 0',
        )
        self.assertEqual(
            _rename_in_expression("priced > 0", "price", "cost"),
            "priced > 0",
        )


class TestForeignKeyRestoreWithoutTargetColumns(unittest.TestCase):
    def test_an_implicit_primary_key_reference_renders_without_a_list(self):
        from sustained.autogenerate import _introspected_fk_sql

        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        fk = IntrospectedForeignKey(
            columns=("owner_id",), target_table="owners", target_columns=()
        )
        sql = _introspected_fk_sql(compiler, "items", "fk_items_owner", fk)
        self.assertEqual(
            sql,
            "ALTER TABLE items ADD CONSTRAINT fk_items_owner "
            "FOREIGN KEY (owner_id) REFERENCES owners",
        )

    def test_an_unknown_target_table_stays_irreversible(self):
        from sustained.autogenerate import _introspected_fk_sql

        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        fk = IntrospectedForeignKey(
            columns=("owner_id",), target_table="?", target_columns=()
        )
        self.assertIsNone(_introspected_fk_sql(compiler, "items", "fk_items_owner", fk))


class TestDuplicateConstraintNames(unittest.TestCase):
    def test_diff_schema_rejects_a_reused_name(self):
        model = make_model(
            "DupC",
            "dup_items",
            {"id": Integer(primary_key=True), "price": Integer()},
        )
        model.tableConstraints = [
            Check("ck_dup", "price > 0"),
            Check("CK_DUP", "price < 100"),
        ]
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(ValueError) as caught:
                diff_schema(conn, [model])
        finally:
            conn.close()
        self.assertIn("CK_DUP", str(caught.exception))


class RoutingCursor:
    """Serves canned catalog rows for Postgres-dialect constraint tests."""

    def __init__(self, routes):
        self._routes = routes
        self._current = []

    def execute(self, sql, params=()):
        self._current = []
        for marker, rows in self._routes.items():
            if marker in sql:
                self._current = rows
                return

    def fetchall(self):
        return self._current

    def close(self):
        pass


class RoutingConnection:
    def __init__(self, routes):
        self._cursor = RoutingCursor(routes)

    def cursor(self):
        return self._cursor


# pg_constraint reports the source table first and spells the referential
# actions as single characters.
_ACTION_CODES = {
    "NO ACTION": "a",
    "RESTRICT": "r",
    "CASCADE": "c",
    "SET NULL": "n",
    "SET DEFAULT": "d",
}


def pg_fk_row(
    cname,
    table,
    column,
    ref_table,
    ref_column,
    delete_rule="NO ACTION",
    update_rule="NO ACTION",
):
    return (
        table,
        cname,
        column,
        ref_table,
        ref_column,
        _ACTION_CODES[delete_rule],
        _ACTION_CODES[update_rule],
    )


def pg_connection(fk_rows=(), check_rows=()):
    columns = [
        ("pg_items", "id", "integer", "int4", None, None, None, "NO", None),
        ("pg_items", "owner_id", "integer", "int4", None, None, None, "YES", None),
        ("pg_items", "price", "integer", "int4", None, None, None, "YES", None),
        ("pg_owners", "id", "integer", "int4", None, None, None, "NO", None),
    ]
    return RoutingConnection(
        {
            "information_schema.columns": columns,
            "pg_catalog.pg_index": [],
            "pg_catalog.pg_constraint": list(fk_rows),
            "check_constraints": list(check_rows),
            "pg_catalog.pg_enum": [],
        }
    )


def pg_models(constraints):
    owner = make_model("PgO", "pg_owners", {"id": Integer(primary_key=True)})
    owner.set_dialect(Dialects.POSTGRES)
    item = make_model(
        "PgI",
        "pg_items",
        {
            "id": Integer(primary_key=True),
            "owner_id": Integer(),
            "price": Integer(),
        },
    )
    item.set_dialect(Dialects.POSTGRES)
    item.tableConstraints = constraints
    return owner, item


class TestPostgresConstraintGeneration(unittest.TestCase):
    def test_missing_foreign_key_adds_constraint(self):
        owner, item = pg_models(
            [
                ForeignKey(
                    "fk_items_owner",
                    "owner_id",
                    "pg_owners.id",
                    on_delete="SET NULL",
                )
            ]
        )
        migration = autogenerate(
            pg_connection(), [owner, item], id="m1", dialect=Dialects.POSTGRES
        )
        self.assertEqual(
            migration.up,
            [
                'ALTER TABLE "pg_items" ADD CONSTRAINT "fk_items_owner" '
                'FOREIGN KEY ("owner_id") REFERENCES "pg_owners" ("id") '
                "ON DELETE SET NULL"
            ],
        )
        self.assertEqual(
            migration.down,
            ['ALTER TABLE "pg_items" DROP CONSTRAINT "fk_items_owner"'],
        )

    def test_missing_check_adds_constraint(self):
        owner, item = pg_models([Check("ck_items_price", "price > 0")])
        migration = autogenerate(
            pg_connection(), [owner, item], id="m1", dialect=Dialects.POSTGRES
        )
        self.assertEqual(
            migration.up,
            [
                'ALTER TABLE "pg_items" ADD CONSTRAINT "ck_items_price" '
                "CHECK (price > 0)"
            ],
        )
        self.assertEqual(
            migration.down,
            ['ALTER TABLE "pg_items" DROP CONSTRAINT "ck_items_price"'],
        )

    def test_matching_constraints_generate_nothing(self):
        owner, item = pg_models(
            [
                ForeignKey("fk_items_owner", "owner_id", "pg_owners.id"),
                Check("ck_items_price", "price > 0"),
            ]
        )
        conn = pg_connection(
            fk_rows=[
                pg_fk_row("fk_items_owner", "pg_items", "owner_id", "pg_owners", "id")
            ],
            check_rows=[("pg_items", "ck_items_price", "(price > 0)")],
        )
        self.assertIsNone(
            autogenerate(conn, [owner, item], id="m1", dialect=Dialects.POSTGRES)
        )

    def test_rewritten_check_stays_a_note(self):
        owner, item = pg_models([Check("ck_items_price", "price > 0")])
        conn = pg_connection(
            check_rows=[("pg_items", "ck_items_price", "((price)::numeric > 0.0)")]
        )
        diff = diff_schema(conn, [owner, item], dialect=Dialects.POSTGRES)
        self.assertEqual(diff.new_checks, [])
        self.assertEqual(diff.changed_checks, [])
        self.assertTrue(any("ck_items_price" in note for note in diff.constraint_notes))
        migration = autogenerate(
            conn, [owner, item], id="m1", dialect=Dialects.POSTGRES
        )
        self.assertIsNone(migration)

    def test_changed_foreign_key_needs_allow_drops(self):
        owner, item = pg_models(
            [
                ForeignKey(
                    "fk_items_owner",
                    "owner_id",
                    "pg_owners.id",
                    on_delete="CASCADE",
                )
            ]
        )
        fk_rows = [
            pg_fk_row("fk_items_owner", "pg_items", "owner_id", "pg_owners", "id")
        ]
        conn = pg_connection(fk_rows=fk_rows)
        diff = diff_schema(conn, [owner, item], dialect=Dialects.POSTGRES)
        self.assertEqual(len(diff.changed_foreign_keys), 1)
        self.assertIn("change foreign key fk_items_owner", diff.summary())
        self.assertIsNone(
            autogenerate(conn, [owner, item], id="m1", dialect=Dialects.POSTGRES)
        )
        migration = autogenerate(
            pg_connection(fk_rows=fk_rows),
            [owner, item],
            id="m2",
            dialect=Dialects.POSTGRES,
            allow_drops=True,
        )
        self.assertEqual(
            migration.up,
            [
                'ALTER TABLE "pg_items" DROP CONSTRAINT "fk_items_owner"',
                'ALTER TABLE "pg_items" ADD CONSTRAINT "fk_items_owner" '
                'FOREIGN KEY ("owner_id") REFERENCES "pg_owners" ("id") '
                "ON DELETE CASCADE",
            ],
        )
        self.assertEqual(
            migration.down,
            [
                'ALTER TABLE "pg_items" DROP CONSTRAINT "fk_items_owner"',
                'ALTER TABLE "pg_items" ADD CONSTRAINT "fk_items_owner" '
                'FOREIGN KEY ("owner_id") REFERENCES "pg_owners" ("id")',
            ],
        )

    def test_extra_constraints_refuse_then_drop(self):
        owner, item = pg_models([])
        fk_rows = [
            pg_fk_row(
                "fk_hand_made",
                "pg_items",
                "owner_id",
                "pg_owners",
                "id",
                delete_rule="CASCADE",
            )
        ]
        check_rows = [("pg_items", "ck_hand_made", "(price < 100)")]
        with self.assertRaises(ValueError) as caught:
            autogenerate(
                pg_connection(fk_rows=fk_rows, check_rows=check_rows),
                [owner, item],
                id="m1",
                dialect=Dialects.POSTGRES,
            )
        self.assertIn("fk_hand_made", str(caught.exception))
        self.assertIn("ck_hand_made", str(caught.exception))
        self.assertIsNone(
            autogenerate(
                pg_connection(fk_rows=fk_rows, check_rows=check_rows),
                [owner, item],
                id="m2",
                dialect=Dialects.POSTGRES,
                ignore_undeclared=True,
            )
        )
        migration = autogenerate(
            pg_connection(fk_rows=fk_rows, check_rows=check_rows),
            [owner, item],
            id="m3",
            dialect=Dialects.POSTGRES,
            allow_drops=True,
        )
        self.assertEqual(
            migration.up,
            [
                'ALTER TABLE "pg_items" DROP CONSTRAINT "fk_hand_made"',
                'ALTER TABLE "pg_items" DROP CONSTRAINT "ck_hand_made"',
            ],
        )
        self.assertEqual(
            migration.down,
            [
                'ALTER TABLE "pg_items" ADD CONSTRAINT "ck_hand_made" '
                "CHECK ((price < 100))",
                'ALTER TABLE "pg_items" ADD CONSTRAINT "fk_hand_made" '
                'FOREIGN KEY ("owner_id") REFERENCES "pg_owners" ("id") '
                "ON DELETE CASCADE",
            ],
        )


class TestUnknownTargetComparison(unittest.TestCase):
    def test_question_mark_target_skips_target_and_actions(self):
        from sustained.autogenerate import _fk_matches

        declared = ForeignKey("fk_x", "owner_id", "owners.id", on_delete="CASCADE")
        actual = IntrospectedForeignKey(columns=("owner_id",), target_table="?")
        self.assertTrue(_fk_matches(declared, actual))

    def test_known_target_compares_actions(self):
        from sustained.autogenerate import _fk_matches

        declared = ForeignKey("fk_x", "owner_id", "owners.id", on_delete="CASCADE")
        actual = IntrospectedForeignKey(
            columns=("owner_id",),
            target_table="owners",
            target_columns=("id",),
            on_delete="NO ACTION",
        )
        self.assertFalse(_fk_matches(declared, actual))


class TestDegradedReadDiffsNothing(unittest.TestCase):
    def test_checks_not_diffed_without_a_check_read(self):
        # MSSQL introspects through the generic information_schema plan,
        # which never reads checks, so a declared Check must not report
        # as missing there.
        columns = [
            ("ms_items", "id", "int", "NO", None),
            ("ms_items", "price", "int", "YES", None),
        ]
        conn = RoutingConnection({"information_schema.columns": columns})
        model = make_model(
            "MsI",
            "ms_items",
            {"id": Integer(primary_key=True), "price": Integer()},
        )
        model.set_dialect(Dialects.MSSQL)
        model.tableConstraints = [Check("ck_ms_price", "price > 0")]
        diff = diff_schema(conn, [model], dialect=Dialects.MSSQL)
        self.assertEqual(diff.new_checks, [])
        self.assertEqual(diff.new_foreign_keys, [])


class TestOutstandingLines(unittest.TestCase):
    def test_unadded_constraints_are_outstanding(self):
        owner, item = pg_models(
            [
                ForeignKey("fk_items_owner", "owner_id", "pg_owners.id"),
                Check("ck_items_price", "price > 0"),
            ]
        )
        diff = diff_schema(pg_connection(), [owner, item], dialect=Dialects.POSTGRES)
        lines = diff.outstanding()
        self.assertIn("foreign key 'fk_items_owner' on 'pg_items' was not added", lines)
        self.assertIn("check 'ck_items_price' on 'pg_items' was not added", lines)


if __name__ == "__main__":
    unittest.main()
