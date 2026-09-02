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
from sustained.introspect import (
    introspect_schema,
    is_sequence_default,
    normalize_default,
)
from sustained.model import Model
from sustained.schema import Index, Integer, Numeric, String, Text


class FakeCursor:
    """Serves canned Postgres catalog rows, and records the SQL asked for."""

    def __init__(
        self,
        columns=(),
        indexes=None,
        foreign_keys=None,
        checks=None,
        enums=None,
        comments=None,
    ):
        self.columns = list(columns)
        self.indexes = indexes
        self.foreign_keys = foreign_keys
        self.checks = checks
        self.enums = enums
        self.comments = comments
        self.statements = []
        self._current = []

    def execute(self, sql, params=()):
        statement = " ".join(sql.split())
        # A guarded read takes a savepoint around every query. The tests
        # look at the queries themselves.
        if not statement.split()[0].upper() in ("SAVEPOINT", "RELEASE", "ROLLBACK"):
            self.statements.append(statement)
        if "information_schema.columns" in sql:
            self._current = self.columns
        elif "pg_catalog.pg_index" in sql:
            if self.indexes is None:
                raise RuntimeError("no pg_index here")
            self._current = self.indexes
        elif "pg_catalog.pg_constraint" in sql:
            if self.foreign_keys is None:
                raise RuntimeError("no pg_constraint here")
            self._current = self.foreign_keys
        elif "check_constraints" in sql:
            if self.checks is None:
                raise RuntimeError("no check views here")
            self._current = self.checks
        elif "pg_enum" in sql:
            if self.enums is None:
                raise RuntimeError("no pg_enum here")
            self._current = self.enums
        elif "pg_description" in sql:
            if self.comments is None:
                raise RuntimeError("no pg_description here")
            self._current = self.comments
        else:
            self._current = []

    def fetchall(self):
        return self._current

    def close(self):
        pass


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


# pg_constraint spells the referential actions as single characters.
_ACTION_CODES = {
    "NO ACTION": "a",
    "RESTRICT": "r",
    "CASCADE": "c",
    "SET NULL": "n",
    "SET DEFAULT": "d",
}


def fk_row(
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
            foreign_keys=[
                fk_row("fk_shows_venue", "shows", "venue_id", "venues", "id")
            ],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["shows"].foreign_key_targets["venue_id"], "venues.id")

    def test_foreign_keys_carry_name_and_actions(self):
        cursor = FakeCursor(
            columns=[column_row("shows", "venue_id", "integer")],
            foreign_keys=[
                fk_row(
                    "fk_shows_venue",
                    "shows",
                    "venue_id",
                    "venues",
                    "id",
                    delete_rule="CASCADE",
                    update_rule="SET NULL",
                )
            ],
        )
        schema = self.read(cursor)
        fk = schema["shows"].foreign_keys["fk_shows_venue"]
        self.assertEqual(fk.columns, ("venue_id",))
        self.assertEqual(fk.target_table, "venues")
        self.assertEqual(fk.target_columns, ("id",))
        self.assertEqual(fk.on_delete, "CASCADE")
        self.assertEqual(fk.on_update, "SET NULL")

    def test_a_composite_foreign_key_keeps_its_column_order(self):
        cursor = FakeCursor(
            columns=[
                column_row("seats", "show_id", "integer"),
                column_row("seats", "venue_id", "integer"),
            ],
            foreign_keys=[
                fk_row("fk_seats_show", "seats", "show_id", "shows", "id"),
                fk_row("fk_seats_show", "seats", "venue_id", "shows", "venue_id"),
            ],
        )
        schema = self.read(cursor)
        fk = schema["seats"].foreign_keys["fk_seats_show"]
        self.assertEqual(fk.columns, ("show_id", "venue_id"))
        self.assertEqual(fk.target_columns, ("id", "venue_id"))
        self.assertEqual(
            schema["seats"].foreign_key_targets,
            {"show_id": "shows.id", "venue_id": "shows.venue_id"},
        )

    def test_same_named_keys_on_two_tables_stay_apart(self):
        # A constraint name is unique per table, not per schema. Two
        # tables can each hold a key called fk_owner.
        cursor = FakeCursor(
            columns=[
                column_row("shows", "owner_id", "integer"),
                column_row("venues", "owner_id", "integer"),
            ],
            foreign_keys=[
                fk_row("fk_owner", "shows", "owner_id", "people", "id"),
                fk_row("fk_owner", "venues", "owner_id", "firms", "id"),
            ],
        )
        schema = self.read(cursor)
        shows = schema["shows"].foreign_keys["fk_owner"]
        venues = schema["venues"].foreign_keys["fk_owner"]
        self.assertEqual(shows.columns, ("owner_id",))
        self.assertEqual(shows.target_table, "people")
        self.assertEqual(venues.columns, ("owner_id",))
        self.assertEqual(venues.target_table, "firms")

    def test_an_unknown_action_character_passes_through(self):
        cursor = FakeCursor(
            columns=[column_row("shows", "venue_id", "integer")],
            foreign_keys=[
                ("shows", "fk_shows_venue", "venue_id", "venues", "id", "x", None)
            ],
        )
        schema = self.read(cursor)
        fk = schema["shows"].foreign_keys["fk_shows_venue"]
        self.assertEqual(fk.on_delete, "X")
        self.assertIsNone(fk.on_update)

    def test_check_constraints_are_read(self):
        cursor = FakeCursor(
            columns=[column_row("shows", "seats", "integer")],
            checks=[("shows", "ck_shows_seats", "((seats > 0))")],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["shows"].checks, {"ck_shows_seats": "((seats > 0))"})

    def test_system_not_null_checks_are_left_out(self):
        cursor = FakeCursor(
            columns=[column_row("shows", "seats", "integer", nullable="NO")],
            checks=[
                ("shows", "2200_16389_1_not_null", "seats IS NOT NULL"),
                ("shows", "ck_shows_seats", "((seats > 0))"),
            ],
        )
        schema = self.read(cursor)
        self.assertEqual(list(schema["shows"].checks), ["ck_shows_seats"])

    def test_enum_types_and_their_columns_are_read(self):
        cursor = FakeCursor(
            columns=[column_row("posts", "status", "USER-DEFINED", "post_status")],
            enums=[
                ("post_status", "draft"),
                ("post_status", "published"),
            ],
        )
        schema = self.read(cursor)
        self.assertEqual(schema.enum_types, {"post_status": ("draft", "published")})
        column = schema["posts"].columns["status"]
        self.assertEqual(column.enum_name, "post_status")
        self.assertEqual(column.enum_values, ("draft", "published"))

    def test_an_enum_value_list_keeps_its_sort_order(self):
        cursor = FakeCursor(
            columns=[column_row("posts", "mood", "USER-DEFINED", "mood")],
            enums=[("mood", "sad"), ("mood", "ok"), ("mood", "happy")],
        )
        schema = self.read(cursor)
        self.assertEqual(schema.enum_types["mood"], ("sad", "ok", "happy"))

    def test_column_comments_are_read(self):
        cursor = FakeCursor(
            columns=[column_row("users", "email", "text")],
            comments=[("users", "email", "Login address")],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["users"].columns["email"].comment, "Login address")
        self.assertTrue(schema.comments_read)
        comment_sql = next(s for s in cursor.statements if "pg_description" in s)
        self.assertIn("d.objsubid > 0", comment_sql)

    def test_a_comment_on_an_unknown_column_is_skipped(self):
        cursor = FakeCursor(
            columns=[column_row("users", "email", "text")],
            comments=[("users", "gone", "orphan"), ("gone", "email", "orphan")],
        )
        schema = self.read(cursor)
        self.assertIsNone(schema["users"].columns["email"].comment)

    def test_a_missing_pg_description_degrades(self):
        cursor = FakeCursor(columns=[column_row("users", "email", "text")])
        schema = self.read(cursor)
        self.assertIsNone(schema["users"].columns["email"].comment)
        self.assertFalse(schema.comments_read)

    def test_missing_catalog_views_degrade_to_columns(self):
        cursor = FakeCursor(columns=[column_row("users", "id", "integer")])
        schema = self.read(cursor)
        self.assertEqual(schema["users"].primary_key, ())
        self.assertEqual(dict(schema["users"].foreign_keys), {})
        self.assertEqual(dict(schema["users"].checks), {})
        self.assertEqual(schema.enum_types, {})
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
            foreign_keys=[
                fk_row("fk_shows_venue", "shows", "venue_id", "venues", "id")
            ],
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
            foreign_keys=[fk_row("fk_shows_venue", "shows", "venue_id", "halls", "id")],
        )
        diff = diff_schema(FakeConnection(cursor), [model], dialect=Dialects.POSTGRES)
        self.assertEqual(
            diff.constraint_notes,
            [
                "shows.venue_id foreign key targets halls.id, "
                "model declares venues.id"
            ],
        )


class TestMixedCaseColumnNames(unittest.TestCase):
    """
    The catalog reports column names lowercased. A model that spells a
    column with capitals must still match its own objects.
    """

    def model(self):
        return make_model(
            "PgMixed",
            "members",
            {
                "id": Integer(primary_key=True, autoincrement=True),
                "Email": String(120, unique=True),
            },
        )

    def catalog(self):
        return FakeCursor(
            columns=[
                column_row("members", "id", "integer", nullable="NO"),
                column_row(
                    "members",
                    "email",
                    "character varying",
                    "varchar",
                    char_length=120,
                ),
            ],
            indexes=[
                ("members", "members_pkey", True, True, "id"),
                ("members", "members_email_key", True, False, "email"),
            ],
            foreign_keys=[],
        )

    def test_the_unique_index_behind_it_is_not_extra(self):
        diff = diff_schema(
            FakeConnection(self.catalog()), [self.model()], dialect=Dialects.POSTGRES
        )
        self.assertEqual(diff.extra_indexes, [])
        self.assertEqual(diff.constraint_notes, [])

    def test_no_drop_is_generated_under_allow_drops(self):
        migration = autogenerate(
            FakeConnection(self.catalog()),
            [self.model()],
            id="clean",
            dialect=Dialects.POSTGRES,
            allow_drops=True,
        )
        self.assertIsNone(migration)


class TestNewTableForeignKeys(unittest.TestCase):
    """
    Postgres can add a constraint to a table that already exists, so a
    new table's foreign keys run as their own statements after every
    create. Two new tables may then point at each other.
    """

    def empty(self):
        return FakeConnection(FakeCursor(indexes=[], foreign_keys=[]))

    def test_a_reference_runs_after_the_creates(self):
        show = make_model("PgOShow", "pg_shows", {"id": Integer(primary_key=True)})
        ticket = make_model(
            "PgOTicket",
            "pg_tickets",
            {
                "id": Integer(primary_key=True),
                "show_id": Integer(references="pg_shows.id"),
            },
        )
        migration = autogenerate(
            self.empty(), [ticket, show], id="create", dialect=Dialects.POSTGRES
        )
        self.assertEqual(
            migration.up,
            [
                'CREATE TABLE "pg_shows" ("id" INTEGER PRIMARY KEY)',
                'CREATE TABLE "pg_tickets" '
                '("id" INTEGER PRIMARY KEY, "show_id" INTEGER)',
                'ALTER TABLE "pg_tickets" ADD CONSTRAINT '
                '"fk_pg_tickets_show_id" FOREIGN KEY ("show_id") '
                'REFERENCES "pg_shows" ("id")',
            ],
        )
        self.assertEqual(
            migration.down,
            [
                'ALTER TABLE "pg_tickets" DROP CONSTRAINT "fk_pg_tickets_show_id"',
                'DROP TABLE IF EXISTS "pg_tickets"',
                'DROP TABLE IF EXISTS "pg_shows"',
            ],
        )

    def test_two_tables_may_point_at_each_other(self):
        left = make_model(
            "PgCycLeft",
            "pg_left",
            {
                "id": Integer(primary_key=True),
                "right_id": Integer(references="pg_right.id"),
            },
        )
        right = make_model(
            "PgCycRight",
            "pg_right",
            {
                "id": Integer(primary_key=True),
                "left_id": Integer(references="pg_left.id"),
            },
        )
        diff = diff_schema(self.empty(), [left, right], dialect=Dialects.POSTGRES)
        self.assertEqual(diff.constraint_notes, [])
        migration = autogenerate(
            self.empty(), [left, right], id="create", dialect=Dialects.POSTGRES
        )
        self.assertEqual(
            [step.split(" ")[0] for step in migration.up],
            ["CREATE", "CREATE", "ALTER", "ALTER"],
        )


class TestNormalizeDefault(unittest.TestCase):
    def test_a_sequence_default_has_nothing_to_compare(self):
        self.assertTrue(is_sequence_default("nextval('users_id_seq'::regclass)"))
        self.assertIsNone(normalize_default("nextval('users_id_seq'::regclass)"))
        self.assertFalse(is_sequence_default(None))
        self.assertFalse(is_sequence_default("5"))

    def test_unbalanced_parentheses_stay(self):
        self.assertEqual(normalize_default("(1)+(2)"), "(1)+(2)")

    def test_balanced_parentheses_come_off(self):
        self.assertEqual(normalize_default("((5))"), "5")

    def test_a_cast_comes_off_before_the_quotes(self):
        self.assertEqual(normalize_default("'x'::character varying(255)"), "X")
        self.assertEqual(normalize_default("'a'::text[]"), "A")
        self.assertEqual(normalize_default("'{}'::jsonb"), "{}")

    def test_an_empty_argument_list_comes_off(self):
        self.assertEqual(
            normalize_default("current_timestamp()"),
            normalize_default("CURRENT_TIMESTAMP"),
        )


class TestSerialColumnDrift(unittest.TestCase):
    """
    A legacy serial column reports nextval() as its default. The model
    declares no default, and the two can never be made equal, so the
    diff must not report it forever.
    """

    def test_a_serial_default_reports_no_note(self):
        cursor = FakeCursor(
            columns=[
                column_row(
                    "legacy",
                    "id",
                    "integer",
                    nullable="NO",
                    default="nextval('legacy_id_seq'::regclass)",
                )
            ],
            indexes=[("legacy", "legacy_pkey", True, True, "id")],
            foreign_keys=[],
        )
        model = make_model(
            "PgLegacy", "legacy", {"id": Integer(primary_key=True, autoincrement=True)}
        )
        diff = diff_schema(FakeConnection(cursor), [model], dialect=Dialects.POSTGRES)
        self.assertEqual(diff.constraint_notes, [])
        self.assertTrue(diff.is_empty())


class TestPostgresSchemaScope(unittest.TestCase):
    """
    Every read keys on the bare table name, so an unscoped read merges
    app.users into public.users and the diff never converges. The read
    covers the schema the connection is on, plus the schemas the models
    name.
    """

    def statements(self, models):
        cursor = FakeCursor(
            columns=[column_row("users", "id", "integer", nullable="NO")],
            indexes=[],
            foreign_keys=[],
            checks=[],
            enums=[],
            comments=[],
        )
        diff_schema(FakeConnection(cursor), models, dialect=Dialects.POSTGRES)
        return cursor.statements

    def model(self, name, schema=None):
        model = make_model(name, "users", {"id": Integer(primary_key=True)})
        model.tableSchema = schema
        return model

    def test_the_read_covers_the_connection_schema(self):
        for sql in self.statements([self.model("PgScopeA")]):
            self.assertIn("= current_schema()", sql)

    def test_a_declared_schema_widens_the_read(self):
        # The declared schema is its own IN list, with the current schema
        # compared beside it. current_schema() returns NULL when the
        # first search_path entry does not exist, and a NULL inside the
        # IN list would match nothing at all.
        for sql in self.statements([self.model("PgScopeB", "app")]):
            self.assertIn("IN ('app')", sql)
            self.assertIn("OR", sql)
            self.assertIn("= current_schema()", sql)

    def test_a_quote_in_a_schema_name_is_escaped(self):
        for sql in self.statements([self.model("PgScopeC", "o'brien")]):
            self.assertIn("IN ('o''brien')", sql)

    def test_the_current_schema_never_stands_in_an_in_list(self):
        # current_schema() returns NULL when the first search_path entry
        # names a schema that does not exist. Inside an IN list beside a
        # declared schema, that NULL would match nothing and the read
        # would come back empty, so every model would look missing.
        for sql in self.statements([self.model("PgScopeF", "app")]):
            self.assertNotIn("IN (current_schema()", sql)

    def test_two_models_on_one_table_name_are_refused(self):
        models = [self.model("PgScopeD", "app"), self.model("PgScopeE", "public")]
        with self.assertRaises(ValueError) as caught:
            diff_schema(FakeConnection(FakeCursor()), models, dialect=Dialects.POSTGRES)
        message = str(caught.exception)
        self.assertIn("schema app", message)
        self.assertIn("schema public", message)


class SavepointCursor:
    """A cursor that records every statement and fails one named query."""

    def __init__(self, failing="pg_catalog.pg_index", savepoints=True):
        self.failing = failing
        self.savepoints = savepoints
        self.statements = []
        self.doomed = False
        self._current = []

    def execute(self, sql, params=()):
        statement = " ".join(sql.split())
        self.statements.append(statement)
        word = statement.split()[0].upper()
        if word == "SAVEPOINT":
            if not self.savepoints:
                raise RuntimeError("no transaction is active")
            return
        if word == "ROLLBACK":
            self.doomed = False
            return
        if word == "RELEASE":
            return
        if self.doomed:
            raise RuntimeError("current transaction is aborted")
        if self.failing in sql:
            self.doomed = True
            raise RuntimeError("no such catalog")
        self._current = []

    def fetchall(self):
        return self._current

    def close(self):
        pass


class TestGuardedRead(unittest.TestCase):
    """
    A Postgres read tries a catalog and falls back when it is not there.
    Postgres refuses every later statement in a transaction once one has
    failed, so each query runs inside a savepoint.
    """

    def test_a_failed_query_rolls_back_to_the_savepoint(self):
        cursor = SavepointCursor()
        introspect_schema(FakeConnection(cursor), Dialects.POSTGRES)
        self.assertIn("ROLLBACK TO SAVEPOINT sustained_read", cursor.statements)
        self.assertFalse(cursor.doomed)
        # The read kept going after the failure.
        self.assertTrue(
            any("pg_catalog.pg_constraint" in s for s in cursor.statements),
            cursor.statements,
        )

    def test_a_read_without_savepoints_still_finishes(self):
        cursor = SavepointCursor(savepoints=False)
        introspect_schema(FakeConnection(cursor), Dialects.POSTGRES)
        self.assertEqual([s for s in cursor.statements if s.startswith("RELEASE")], [])
        self.assertTrue(
            any("information_schema.columns" in s for s in cursor.statements)
        )

    def test_a_rollback_that_fails_keeps_the_first_error(self):
        class NoRollback(SavepointCursor):
            def execute(self, sql, params=()):
                if sql.startswith("ROLLBACK"):
                    self.statements.append(sql)
                    raise RuntimeError("no savepoint to roll back to")
                super().execute(sql, params)

        cursor = NoRollback()
        # The plan degrades on the error rather than raising it.
        introspect_schema(FakeConnection(cursor), Dialects.POSTGRES)
        self.assertIn("ROLLBACK TO SAVEPOINT sustained_read", cursor.statements)

    def test_a_dialect_that_keeps_its_transaction_takes_no_savepoint(self):
        cursor = SavepointCursor(failing="never")
        introspect_schema(FakeConnection(cursor), Dialects.MYSQL)
        self.assertEqual(
            [s for s in cursor.statements if s.startswith("SAVEPOINT")], []
        )


class StackingCursor:
    """
    A cursor that keeps the savepoint stack, so a read that forgets a
    release leaves the stack deep. Postgres takes a duplicate savepoint
    name, and ROLLBACK TO SAVEPOINT leaves the name on the stack.
    """

    def __init__(self, failing="pg_catalog", releases=True):
        self.failing = failing
        self.releases = releases
        self.stack = []
        self.statements = []
        self.failures = 0
        self.doomed = False
        self._current = []

    def execute(self, sql, params=()):
        statement = " ".join(sql.split())
        self.statements.append(statement)
        word = statement.split()[0].upper()
        if word == "SAVEPOINT":
            self.stack.append(statement.split()[1])
            return
        if word == "ROLLBACK":
            self.doomed = False
            return
        if word == "RELEASE":
            if not self.releases:
                raise RuntimeError("this driver has no RELEASE SAVEPOINT")
            if self.stack:
                self.stack.pop()
            return
        if self.doomed:
            raise RuntimeError("current transaction is aborted")
        if self.failing in statement:
            self.doomed = True
            self.failures += 1
            raise RuntimeError("no such catalog")
        self._current = (
            [column_row("users", "id", "integer")]
            if "information_schema.columns" in statement
            else []
        )

    def fetchall(self):
        return self._current

    def close(self):
        pass


class TestSavepointStack(unittest.TestCase):
    """
    A guarded read releases the savepoint it takes, and keeps its rows
    when the driver refuses the release.
    """

    def test_a_failed_query_leaves_no_savepoint_behind(self):
        cursor = StackingCursor()
        introspect_schema(FakeConnection(cursor), Dialects.POSTGRES)
        self.assertGreater(cursor.failures, 1)
        self.assertEqual(cursor.stack, [])

    def test_a_driver_that_refuses_the_release_keeps_the_rows(self):
        cursor = StackingCursor(failing="never", releases=False)
        schema = introspect_schema(FakeConnection(cursor), Dialects.POSTGRES)
        self.assertEqual(list(schema), ["users"])

    def test_a_refused_release_after_a_failure_keeps_the_read(self):
        cursor = StackingCursor(releases=False)
        schema = introspect_schema(FakeConnection(cursor), Dialects.POSTGRES)
        self.assertEqual(list(schema), ["users"])
        self.assertIn("ROLLBACK TO SAVEPOINT sustained_read", cursor.statements)


if __name__ == "__main__":
    unittest.main()
