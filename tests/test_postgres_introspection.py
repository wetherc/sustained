"""
Tests for reading a Postgres schema, and for the drift a model diffs
against it. Postgres has its own reading plan: the generic
information_schema read loses varchar lengths and numeric precision,
reports enum columns as USER-DEFINED, resolves foreign key targets to
'?', and sees no index that is not a UNIQUE constraint.
"""

import unittest

from sustained.autogenerate import autogenerate, diff_schema
from sustained.dialects import Dialects
from sustained.introspect import introspect_schema
from sustained.model import Model
from sustained.schema import Index, Integer, Numeric, String, Text


class FakeCursor:
    """Serves canned Postgres catalog rows, and records the SQL asked for."""

    def __init__(self, columns=(), indexes=None, foreign_keys=None):
        self.columns = list(columns)
        self.indexes = indexes
        self.foreign_keys = foreign_keys
        self.statements = []
        self._current = []

    def execute(self, sql, params=()):
        self.statements.append(" ".join(sql.split()))
        if "information_schema.columns" in sql:
            self._current = self.columns
        elif "pg_catalog.pg_index" in sql:
            if self.indexes is None:
                raise RuntimeError("no pg_index here")
            self._current = self.indexes
        elif "referential_constraints" in sql:
            if self.foreign_keys is None:
                raise RuntimeError("no referential views here")
            self._current = self.foreign_keys
        else:
            self._current = []

    def fetchall(self):
        return self._current


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def column_row(
    table,
    name,
    data_type,
    udt_name=None,
    char_length=None,
    precision=None,
    scale=None,
    nullable="YES",
    default=None,
):
    return (
        table,
        name,
        data_type,
        udt_name or data_type,
        char_length,
        precision,
        scale,
        nullable,
        default,
    )


def make_model(name, table, columns, indexes=None):
    return type(
        name,
        (Model,),
        {
            "tableName": table,
            "tableColumns": columns,
            "indexes": indexes or [],
            "_dialect": Dialects.POSTGRES,
        },
    )


class TestPostgresCatalogQueries(unittest.TestCase):
    def read(self, cursor):
        return introspect_schema(FakeConnection(cursor), Dialects.POSTGRES)

    def test_reads_base_tables_only(self):
        cursor = FakeCursor(columns=[column_row("users", "id", "integer")])
        self.read(cursor)
        self.assertIn("table_type = 'BASE TABLE'", cursor.statements[0])

    def test_varchar_keeps_its_length(self):
        cursor = FakeCursor(
            columns=[
                column_row(
                    "users", "email", "character varying", "varchar", char_length=120
                )
            ]
        )
        schema = self.read(cursor)
        self.assertEqual(
            schema["users"].columns["email"].raw_type, "character varying(120)"
        )

    def test_numeric_keeps_precision_and_scale(self):
        cursor = FakeCursor(
            columns=[
                column_row("bills", "amount", "numeric", precision=18, scale=6),
                column_row("bills", "level", "numeric", precision=10),
            ]
        )
        schema = self.read(cursor)
        self.assertEqual(schema["bills"].columns["amount"].raw_type, "numeric(18,6)")
        self.assertEqual(schema["bills"].columns["level"].raw_type, "numeric(10,0)")

    def test_a_bare_numeric_stays_bare(self):
        cursor = FakeCursor(columns=[column_row("bills", "amount", "numeric")])
        schema = self.read(cursor)
        self.assertEqual(schema["bills"].columns["amount"].raw_type, "numeric")

    def test_an_enum_column_reports_its_type_name(self):
        cursor = FakeCursor(
            columns=[column_row("posts", "status", "USER-DEFINED", "post_status")]
        )
        schema = self.read(cursor)
        self.assertEqual(schema["posts"].columns["status"].raw_type, "post_status")

    def test_plain_indexes_are_read(self):
        cursor = FakeCursor(
            columns=[column_row("users", "email", "text")],
            indexes=[("users", "ix_users_email", False, False, "email")],
        )
        schema = self.read(cursor)
        index = schema["users"].indexes["ix_users_email"]
        self.assertEqual(index.columns, ("email",))
        self.assertFalse(index.unique)

    def test_the_primary_key_comes_from_its_index(self):
        cursor = FakeCursor(
            columns=[
                column_row("users", "id", "integer", nullable="NO"),
                column_row("users", "org", "integer", nullable="NO"),
            ],
            indexes=[
                ("users", "users_pkey", True, True, "id"),
                ("users", "users_pkey", True, True, "org"),
            ],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["users"].primary_key, ("id", "org"))
        self.assertTrue(schema["users"].columns["id"].primary_key)
        self.assertNotIn("users_pkey", schema["users"].indexes)

    def test_an_expression_index_is_skipped(self):
        cursor = FakeCursor(
            columns=[column_row("users", "email", "text")],
            indexes=[
                ("users", "ix_lower_email", False, False, None),
                ("users", "ix_plain", False, False, "email"),
            ],
        )
        schema = self.read(cursor)
        self.assertNotIn("ix_lower_email", schema["users"].indexes)
        self.assertIn("ix_plain", schema["users"].indexes)

    def test_a_multi_column_index_keeps_its_order(self):
        cursor = FakeCursor(
            columns=[
                column_row("shows", "venue_id", "integer"),
                column_row("shows", "starts_at", "timestamp without time zone"),
            ],
            indexes=[
                ("shows", "ix_venue_start", False, False, "venue_id"),
                ("shows", "ix_venue_start", False, False, "starts_at"),
            ],
        )
        schema = self.read(cursor)
        self.assertEqual(
            schema["shows"].indexes["ix_venue_start"].columns,
            ("venue_id", "starts_at"),
        )

    def test_foreign_key_targets_resolve(self):
        cursor = FakeCursor(
            columns=[column_row("shows", "venue_id", "integer")],
            foreign_keys=[("shows", "venue_id", "venues", "id")],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["shows"].foreign_keys["venue_id"], "venues.id")

    def test_missing_catalog_views_degrade_to_columns(self):
        cursor = FakeCursor(columns=[column_row("users", "id", "integer")])
        schema = self.read(cursor)
        self.assertEqual(schema["users"].primary_key, ())
        self.assertEqual(dict(schema["users"].foreign_keys), {})
        self.assertIn("id", schema["users"].columns)


class TestPostgresDrift(unittest.TestCase):
    """A model diffed against the catalog rows its own DDL would produce."""

    def catalog(self):
        return FakeCursor(
            columns=[
                column_row("users", "id", "integer", nullable="NO"),
                column_row(
                    "users",
                    "email",
                    "character varying",
                    "varchar",
                    char_length=120,
                    nullable="NO",
                ),
                column_row("users", "bio", "text"),
                column_row("users", "balance", "numeric", precision=18, scale=6),
            ],
            indexes=[
                ("users", "users_pkey", True, True, "id"),
                ("users", "ix_users_email", False, False, "email"),
            ],
            foreign_keys=[],
        )

    def model(self):
        return make_model(
            "PgUser",
            "users",
            {
                "id": Integer(primary_key=True, autoincrement=True),
                "email": String(120, nullable=False),
                "bio": Text(),
                "balance": Numeric(18, 6),
            },
            indexes=[Index("ix_users_email", "email")],
        )

    def diff(self, cursor):
        return diff_schema(
            FakeConnection(cursor), [self.model()], dialect=Dialects.POSTGRES
        )

    def test_a_matching_schema_reports_no_drift(self):
        diff = self.diff(self.catalog())
        self.assertEqual(diff.changed_columns, [])
        self.assertEqual(diff.new_columns, [])
        self.assertEqual(diff.new_indexes, [])
        self.assertEqual(diff.extra_indexes, [])
        self.assertEqual(diff.missing_tables, [])

    def test_a_narrower_varchar_reports_drift(self):
        cursor = self.catalog()
        cursor.columns[1] = column_row(
            "users",
            "email",
            "character varying",
            "varchar",
            char_length=60,
            nullable="NO",
        )
        diff = self.diff(cursor)
        self.assertEqual([c[1] for c in diff.changed_columns], ["email"])

    def test_a_declared_index_the_database_holds_is_not_recreated(self):
        migration = autogenerate(
            FakeConnection(self.catalog()),
            [self.model()],
            id="noop",
            dialect=Dialects.POSTGRES,
        )
        self.assertIsNone(migration)

    def test_a_missing_declared_index_is_created(self):
        cursor = self.catalog()
        cursor.indexes = [("users", "users_pkey", True, True, "id")]
        diff = self.diff(cursor)
        self.assertEqual([i.name for _, i in diff.new_indexes], ["ix_users_email"])

    def test_a_matching_foreign_key_makes_no_note(self):
        model = make_model(
            "PgShow",
            "shows",
            {
                "id": Integer(primary_key=True),
                "venue_id": Integer(references="venues.id"),
            },
        )
        cursor = FakeCursor(
            columns=[
                column_row("shows", "id", "integer", nullable="NO"),
                column_row("shows", "venue_id", "integer"),
            ],
            indexes=[("shows", "shows_pkey", True, True, "id")],
            foreign_keys=[("shows", "venue_id", "venues", "id")],
        )
        diff = diff_schema(FakeConnection(cursor), [model], dialect=Dialects.POSTGRES)
        self.assertEqual(diff.constraint_notes, [])

    def test_a_foreign_key_to_the_wrong_table_makes_a_note(self):
        model = make_model(
            "PgShowWrong",
            "shows",
            {
                "id": Integer(primary_key=True),
                "venue_id": Integer(references="venues.id"),
            },
        )
        cursor = FakeCursor(
            columns=[
                column_row("shows", "id", "integer", nullable="NO"),
                column_row("shows", "venue_id", "integer"),
            ],
            indexes=[("shows", "shows_pkey", True, True, "id")],
            foreign_keys=[("shows", "venue_id", "halls", "id")],
        )
        diff = diff_schema(FakeConnection(cursor), [model], dialect=Dialects.POSTGRES)
        self.assertEqual(
            diff.constraint_notes,
            [
                "shows.venue_id foreign key targets halls.id, "
                "model declares venues.id"
            ],
        )


if __name__ == "__main__":
    unittest.main()
