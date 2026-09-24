"""
Tests for declared table constraints on DuckDB. DuckDB names every
constraint after its table and columns, whatever name CREATE TABLE gave
it, and it takes no ADD or DROP CONSTRAINT on a table that exists. The
diff pairs constraints by content there and reports every difference as
a note.
"""

import unittest

from sustained.autogenerate import _pair_constraints, autogenerate, diff_schema
from sustained.dialects import Dialects
from sustained.introspect import IntrospectedForeignKey, introspect_schema
from sustained.schema import Check, ForeignKey, Integer
from tests.test_autogenerate import make_model

try:
    import duckdb

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False


class TestPairConstraints(unittest.TestCase):
    def test_an_exact_match_is_never_taken_by_a_looser_one(self):
        """
        Two keys on one column that point at different tables pair with
        their own targets, even when the first declared key would also
        pass the column test against the other's row.
        """
        to_a = ForeignKey("fk_a", "ref", "a.id")
        to_b = ForeignKey("fk_b", "ref", "b.id")
        actual = {
            "t_ref_id_fkey": IntrospectedForeignKey(("ref",), "b", ("id",)),
            "t_ref_id_fkey1": IntrospectedForeignKey(("ref",), "a", ("id",)),
        }
        pairs, missing, extra = _pair_constraints(
            [to_a, to_b],
            actual,
            [
                lambda fk, _, row: fk.target_table == row.target_table,
                lambda fk, _, row: fk.columns[0] == row.columns[0],
            ],
        )
        self.assertEqual(
            [(fk.name, row.target_table) for fk, row in pairs],
            [("fk_a", "a"), ("fk_b", "b")],
        )
        self.assertEqual((missing, extra), ([], []))


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class DuckDbConstraintTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        self.Parent = self.model("DcP", "dc_parents", {"id": Integer(primary_key=True)})
        self.Other = self.model("DcO", "dc_others", {"id": Integer(primary_key=True)})
        self.Kid = self.model(
            "DcK",
            "dc_kids",
            {
                "id": Integer(primary_key=True),
                "parent_id": Integer(),
                "q": Integer(),
            },
        )
        self.Kid.tableConstraints = [
            ForeignKey("fk_parent", "parent_id", "dc_parents.id"),
            Check("positive_q", "q > 0"),
            Check("always", "1 = 1"),
        ]

    def tearDown(self):
        self.conn.close()

    def model(self, name, table, columns):
        model = make_model(f"{name}_{self.id().rsplit('.', 1)[-1]}", table, columns)
        model.set_dialect(Dialects.DUCKDB)
        return model

    def models(self):
        return [self.Parent, self.Other, self.Kid]

    def create(self):
        migration = autogenerate(
            self.conn, self.models(), id="create", dialect=Dialects.DUCKDB
        )
        for statement in migration.up:
            self.conn.execute(statement)

    def diff(self):
        return diff_schema(self.conn, self.models(), dialect=Dialects.DUCKDB)


class TestDuckDbConvergence(DuckDbConstraintTestCase):
    def test_created_constraints_read_back_as_declared(self):
        """
        DuckDB reports fk_parent as dc_kids_parent_id_id_fkey, so a pair
        by name read it as missing and generated an ADD CONSTRAINT that
        DuckDB refuses on every later migrate.
        """
        self.create()
        diff = self.diff()
        self.assertTrue(diff.is_empty(), diff.summary())
        self.assertEqual(diff.constraint_notes, [])
        self.assertEqual(diff.outstanding(), [])
        self.assertIsNone(
            autogenerate(self.conn, self.models(), id="again", dialect=Dialects.DUCKDB)
        )

    def test_the_read_reports_targets_and_bare_check_expressions(self):
        self.create()
        table = introspect_schema(self.conn, Dialects.DUCKDB)["dc_kids"]
        self.assertEqual(
            list(table.foreign_keys.values()),
            [IntrospectedForeignKey(("parent_id",), "dc_parents", ("id",))],
        )
        self.assertEqual(sorted(table.checks.values()), ["(1 = 1)", "(q > 0)"])

    def test_a_references_shorthand_key_is_not_extra(self):
        self.Kid.tableColumns["parent_id"] = Integer(references="dc_parents.id")
        self.Kid.tableConstraints = []
        self.create()
        diff = self.diff()
        self.assertTrue(diff.is_empty(), diff.summary())
        self.assertEqual(diff.constraint_notes, [])


class TestDuckDbDifferencesAreNotes(DuckDbConstraintTestCase):
    def assert_notes_only(self, *fragments):
        diff = self.diff()
        self.assertEqual(
            (diff.new_foreign_keys, diff.changed_foreign_keys, diff.extra_foreign_keys),
            ([], [], []),
        )
        self.assertEqual(
            (diff.new_checks, diff.changed_checks, diff.extra_checks), ([], [], [])
        )
        self.assertEqual(diff.outstanding(), [])
        notes = "\n".join(diff.constraint_notes)
        for fragment in fragments:
            self.assertIn(fragment, notes)
        self.assertIn("recreate the table by hand", notes)
        migration = autogenerate(
            self.conn,
            self.models(),
            id="change",
            dialect=Dialects.DUCKDB,
            allow_drops=True,
        )
        self.assertIsNone(migration)

    def test_a_changed_check_is_a_note(self):
        self.create()
        self.Kid.tableConstraints[1] = Check("positive_q", "q > 1")
        self.assert_notes_only(
            "declares check 'positive_q' that the database does not have",
            "has check 'dc_kids_q_check' that no model declares: '(q > 0)'",
        )

    def test_a_new_foreign_key_is_a_note(self):
        self.Kid.tableConstraints = []
        self.create()
        self.Kid.tableConstraints = [
            ForeignKey("fk_parent", "parent_id", "dc_parents.id")
        ]
        self.assert_notes_only(
            "dc_kids declares foreign key 'fk_parent' that the database does not have"
        )

    def test_a_changed_target_is_a_note(self):
        self.create()
        self.Kid.tableConstraints[0] = ForeignKey(
            "fk_parent", "parent_id", "dc_others.id"
        )
        self.assert_notes_only(
            "foreign key 'fk_parent' points at dc_parents, "
            "the model declares dc_others"
        )

    def test_an_undeclared_foreign_key_is_a_note_that_blocks_nothing(self):
        self.create()
        self.Kid.tableConstraints = self.Kid.tableConstraints[1:]
        self.assert_notes_only(
            "has foreign key 'dc_kids_parent_id_id_fkey' on (parent_id) "
            "that no model declares"
        )
        self.assertIsNone(
            autogenerate(self.conn, self.models(), id="plain", dialect=Dialects.DUCKDB)
        )


class FailingConstraintsView:
    """A DuckDB connection whose duckdb_constraints() read raises."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return FailingConstraintsCursor(self._conn.cursor())


class FailingConstraintsCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=()):
        if "duckdb_constraints()" in sql:
            raise duckdb.CatalogException("no duckdb_constraints()")
        return self._cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class TestDuckDbConstraintsFallback(DuckDbConstraintTestCase):
    def test_a_failed_read_keeps_the_information_schema_constraints(self):
        self.create()
        table = introspect_schema(FailingConstraintsView(self.conn), Dialects.DUCKDB)[
            "dc_kids"
        ]
        self.assertEqual(
            list(table.foreign_keys.values()),
            [
                IntrospectedForeignKey(
                    ("parent_id",), "?", name="dc_kids_parent_id_id_fkey"
                )
            ],
        )
        self.assertIn("dc_kids_q_check", table.checks)


if __name__ == "__main__":
    unittest.main()
