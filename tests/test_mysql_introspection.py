"""
Tests for reading a MySQL or MariaDB schema, and for the migration this
diffs against a model.
"""

import unittest

from sustained.autogenerate import autogenerate, diff_schema
from sustained.dialects import Dialects
from sustained.introspect import introspect_schema, normalize_default, normalize_type
from sustained.model import Model
from sustained.schema import (
    BigInteger,
    Boolean,
    Check,
    Integer,
    Json,
    String,
    Text,
    Timestamp,
)


class FakeCursor:
    """Serves canned MySQL catalog rows, and records the SQL asked for."""

    def __init__(
        self,
        columns=(),
        constraints=None,
        checks=None,
        commented_columns=None,
        table_checks=None,
    ):
        self.columns = list(columns)
        self.constraints = constraints
        # Rows of the MariaDB json_valid recovery read: (table, clause).
        self.checks = checks
        # Rows of the shared check read: (table, constraint, clause).
        self.table_checks = table_checks
        # The same column rows the read asks for first, with the comment
        # selected. None means the engine has no column_comment column.
        self.commented_columns = commented_columns
        self.statements = []
        self._current = []

    def execute(self, sql, params=()):
        self.statements.append(" ".join(sql.split()))
        if "information_schema.columns" in sql:
            if "column_comment" in sql:
                if self.commented_columns is None:
                    raise RuntimeError("no column_comment here")
                self._current = self.commented_columns
            else:
                self._current = self.columns
        elif "constraint_type = 'CHECK'" in sql:
            if self.table_checks is None:
                raise RuntimeError("no check view here")
            self._current = self.table_checks
        elif "information_schema.table_constraints" in sql:
            if self.constraints is None:
                raise RuntimeError("no constraint views here")
            self._current = self.constraints
        elif "information_schema.check_constraints" in sql:
            if self.checks is None:
                raise RuntimeError("Unknown column 'table_name'")
            self._current = self.checks
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


def make_model(name, table, columns):
    return type(
        name,
        (Model,),
        {"tableName": table, "tableColumns": columns, "_dialect": Dialects.MYSQL},
    )


class TestMysqlCatalogQueries(unittest.TestCase):
    def read(self, cursor):
        return introspect_schema(FakeConnection(cursor), Dialects.MYSQL)

    def test_reads_column_type_not_data_type(self):
        cursor = FakeCursor(columns=[("users", "email", "varchar(120)", "NO", None)])
        schema = self.read(cursor)
        self.assertEqual(schema["users"].columns["email"].raw_type, "varchar(120)")
        self.assertIn("column_type", cursor.statements[0])
        self.assertNotIn("data_type", cursor.statements[0])

    def test_reads_only_the_connected_database(self):
        cursor = FakeCursor(columns=[("users", "id", "int", "NO", None)])
        self.read(cursor)
        self.assertIn("table_schema = DATABASE()", cursor.statements[0])

    def test_constraint_join_matches_schemas(self):
        cursor = FakeCursor(
            columns=[("users", "id", "int", "NO", None)],
            constraints=[("users", "PRIMARY KEY", "PRIMARY", "id")],
        )
        schema = self.read(cursor)
        self.assertIn("tc.table_schema = kcu.table_schema", cursor.statements[2])
        self.assertEqual(schema["users"].primary_key, ("id",))
        self.assertTrue(schema["users"].columns["id"].primary_key)

    def test_missing_constraint_views_degrade_to_columns(self):
        cursor = FakeCursor(columns=[("users", "id", "int", "NO", None)])
        schema = self.read(cursor)
        self.assertEqual(schema["users"].primary_key, ())
        self.assertIn("id", schema["users"].columns)

    def test_unique_and_foreign_constraints_are_read(self):
        cursor = FakeCursor(
            columns=[
                ("users", "email", "varchar(120)", "NO", None),
                ("users", "venue_id", "int", "YES", None),
            ],
            constraints=[
                ("users", "UNIQUE", "uq_email", "email"),
                ("users", "FOREIGN KEY", "fk_venue", "venue_id"),
            ],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["users"].indexes["uq_email"].columns, ("email",))
        self.assertTrue(schema["users"].indexes["uq_email"].unique)
        fk = schema["users"].foreign_keys["fk_venue"]
        self.assertEqual(fk.columns, ("venue_id",))
        self.assertEqual(fk.target_table, "?")
        self.assertEqual(schema["users"].foreign_key_targets["venue_id"], "?")

    def test_an_enum_column_reports_its_values(self):
        cursor = FakeCursor(
            columns=[("posts", "status", "enum('draft','published')", "NO", None)]
        )
        schema = self.read(cursor)
        column = schema["posts"].columns["status"]
        self.assertEqual(column.raw_type, "enum('draft','published')")
        self.assertEqual(column.enum_values, ("draft", "published"))
        self.assertIsNone(column.enum_name)

    def test_a_quote_inside_an_enum_value_survives(self):
        cursor = FakeCursor(
            columns=[("posts", "mood", "enum('it''s fine','bad')", "YES", None)]
        )
        schema = self.read(cursor)
        self.assertEqual(
            schema["posts"].columns["mood"].enum_values, ("it's fine", "bad")
        )

    def test_a_non_enum_column_has_no_enum_values(self):
        cursor = FakeCursor(columns=[("posts", "title", "varchar(80)", "NO", None)])
        schema = self.read(cursor)
        self.assertEqual(schema["posts"].columns["title"].enum_values, ())


class TestMysqlCommentRead(unittest.TestCase):
    def read(self, cursor):
        return introspect_schema(FakeConnection(cursor), Dialects.MYSQL)

    def test_a_column_comment_is_read(self):
        cursor = FakeCursor(
            commented_columns=[
                ("users", "email", "varchar(120)", "NO", None, "Login address")
            ],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["users"].columns["email"].comment, "Login address")
        self.assertTrue(schema.comments_read)

    def test_an_empty_comment_reads_as_none(self):
        cursor = FakeCursor(
            commented_columns=[("users", "email", "varchar(120)", "NO", None, "")],
        )
        schema = self.read(cursor)
        self.assertIsNone(schema["users"].columns["email"].comment)
        self.assertTrue(schema.comments_read)

    def test_the_columns_are_read_once(self):
        cursor = FakeCursor(
            commented_columns=[
                ("users", "email", "varchar(120)", "NO", None, "Login address")
            ],
        )
        self.read(cursor)
        reads = [s for s in cursor.statements if "information_schema.columns" in s]
        self.assertEqual(len(reads), 1)

    def test_a_failed_comment_read_degrades(self):
        cursor = FakeCursor(columns=[("users", "email", "varchar(120)", "NO", None)])
        schema = self.read(cursor)
        self.assertIsNone(schema["users"].columns["email"].comment)
        self.assertFalse(schema.comments_read)

    def test_only_the_connected_database_is_read(self):
        cursor = FakeCursor(
            commented_columns=[],
        )
        self.read(cursor)
        comment_sql = next(s for s in cursor.statements if "column_comment" in s)
        self.assertIn("table_schema = DATABASE()", comment_sql)


class TestMariadbJson(unittest.TestCase):
    def read(self, cursor):
        return introspect_schema(FakeConnection(cursor), Dialects.MYSQL)

    def test_longtext_with_a_json_check_reads_as_json(self):
        cursor = FakeCursor(
            columns=[("shows", "payload", "longtext", "YES", None)],
            checks=[("shows", "json_valid(`payload`)")],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["shows"].columns["payload"].raw_type, "JSON")

    def test_a_plain_longtext_column_is_left_alone(self):
        cursor = FakeCursor(
            columns=[("shows", "notes", "longtext", "YES", None)],
            checks=[("shows", "json_valid(`payload`)")],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["shows"].columns["notes"].raw_type, "longtext")

    def test_an_unrelated_check_changes_nothing(self):
        cursor = FakeCursor(
            columns=[("shows", "seats", "int", "YES", None)],
            checks=[("shows", "`seats` > 0")],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["shows"].columns["seats"].raw_type, "int")

    def test_a_check_on_an_unknown_table_is_skipped(self):
        cursor = FakeCursor(
            columns=[("shows", "payload", "longtext", "YES", None)],
            checks=[("gone", "json_valid(`payload`)")],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["shows"].columns["payload"].raw_type, "longtext")

    def test_a_json_column_is_not_promoted_twice(self):
        cursor = FakeCursor(
            columns=[("shows", "payload", "json", "YES", None)],
            checks=[("shows", "json_valid(`payload`)")],
        )
        schema = self.read(cursor)
        self.assertEqual(schema["shows"].columns["payload"].raw_type, "json")

    def test_mysql_without_the_view_still_reads_its_columns(self):
        cursor = FakeCursor(columns=[("shows", "payload", "json", "YES", None)])
        schema = self.read(cursor)
        self.assertEqual(schema["shows"].columns["payload"].raw_type, "json")


class TestMysqlChecks(unittest.TestCase):
    """
    MySQL 8.0.16 and MariaDB both fill information_schema.check_constraints,
    so a declared Check diffs there like it does on Postgres.
    """

    def model(self, constraints):
        model = make_model(
            "MysqlChecked",
            "shows",
            {
                "id": Integer(primary_key=True, autoincrement=True),
                "seats": Integer(),
            },
        )
        model.tableConstraints = constraints
        return model

    def cursor(self, table_checks=None, checks=None):
        return FakeCursor(
            columns=[
                ("shows", "id", "int", "NO", None),
                ("shows", "seats", "int", "YES", None),
            ],
            constraints=[("shows", "PRIMARY KEY", "PRIMARY", "id")],
            table_checks=table_checks,
            checks=checks,
        )

    def test_checks_are_read(self):
        schema = introspect_schema(
            FakeConnection(
                self.cursor(table_checks=[("shows", "ck_seats", "(`seats` > 0)")])
            ),
            Dialects.MYSQL,
        )
        self.assertTrue(schema.checks_read)
        self.assertEqual(schema["shows"].checks, {"ck_seats": "(`seats` > 0)"})

    def test_a_missing_check_generates_add_constraint(self):
        model = self.model([Check("ck_seats", "seats > 0")])
        migration = autogenerate(
            FakeConnection(self.cursor(table_checks=[])),
            [model],
            id="m1",
            dialect=Dialects.MYSQL,
        )
        self.assertEqual(
            migration.up,
            ["ALTER TABLE `shows` ADD CONSTRAINT `ck_seats` CHECK (seats > 0)"],
        )
        self.assertEqual(
            migration.down, ["ALTER TABLE `shows` DROP CONSTRAINT `ck_seats`"]
        )

    def test_the_mariadb_json_check_is_not_an_extra(self):
        cursor = FakeCursor(
            columns=[
                ("shows", "id", "int", "NO", None),
                ("shows", "payload", "longtext", "YES", None),
            ],
            constraints=[("shows", "PRIMARY KEY", "PRIMARY", "id")],
            table_checks=[("shows", "payload", "json_valid(`payload`)")],
            checks=[("shows", "json_valid(`payload`)")],
        )
        model = make_model(
            "MysqlJsonChecked",
            "shows",
            {
                "id": Integer(primary_key=True, autoincrement=True),
                "payload": Json(),
            },
        )
        diff = diff_schema(FakeConnection(cursor), [model], dialect=Dialects.MYSQL)
        self.assertEqual(diff.extra_checks, [])
        self.assertTrue(diff.is_empty())


class TestMysqlCheckExpressions(unittest.TestCase):
    """
    MySQL and MariaDB report the clause the engine rewrote, with the
    identifiers quoted. The model declares the bare expression, and a
    difference between the two would stand as a note no migration could
    close.
    """

    def diff(self):
        cursor = FakeCursor(
            columns=[
                ("priced", "id", "int", "NO", None),
                ("priced", "price", "int", "NO", None),
            ],
            constraints=[("priced", "PRIMARY KEY", "PRIMARY", "id")],
            checks=[],
            table_checks=[("priced", "price_positive", "(`price` > 0)")],
        )
        model = make_model(
            "MysqlPriced",
            "priced",
            {
                "id": Integer(primary_key=True, autoincrement=True),
                "price": Integer(nullable=False),
            },
        )
        model.tableConstraints = [Check("price_positive", "price > 0")]
        return diff_schema(FakeConnection(cursor), [model], dialect=Dialects.MYSQL)

    def test_a_quoted_clause_matches_the_declared_expression(self):
        diff = self.diff()
        self.assertEqual(diff.constraint_notes, [])
        self.assertEqual(diff.new_checks, [])
        self.assertEqual(diff.changed_checks, [])
        self.assertTrue(diff.is_empty())


class TestMysqlNormalization(unittest.TestCase):
    def test_sized_text_types_reduce_to_text(self):
        for spelling in ("tinytext", "mediumtext", "longtext"):
            with self.subTest(type=spelling):
                self.assertEqual(normalize_type(spelling), "TEXT")

    def test_tinyint_stays_apart_from_integer(self):
        self.assertEqual(normalize_type("tinyint(1)"), "TINYINT")
        self.assertNotEqual(normalize_type("tinyint"), normalize_type("int"))

    def test_mysql_spellings_reduce_to_the_logical_type(self):
        expected = {
            "int": "INTEGER",
            "bigint": "BIGINT",
            "varchar(120)": "VARCHAR",
            "double": "FLOAT",
            "decimal(18,6)": "NUMERIC",
            "datetime": "TIMESTAMP",
            "json": "JSON",
        }
        for spelling, logical in expected.items():
            with self.subTest(type=spelling):
                self.assertEqual(normalize_type(spelling), logical)

    def test_mariadb_and_mysql_timestamp_defaults_agree(self):
        self.assertEqual(
            normalize_default("current_timestamp()"),
            normalize_default("CURRENT_TIMESTAMP"),
        )


class TestMysqlDrift(unittest.TestCase):
    """A model diffed against the catalog rows its own DDL would produce."""

    def catalog(self):
        return FakeCursor(
            columns=[
                ("users", "id", "int", "NO", None),
                ("users", "email", "varchar(120)", "NO", None),
                ("users", "active", "tinyint(1)", "YES", None),
                ("users", "bio", "text", "YES", None),
                ("users", "seen_at", "datetime", "YES", None),
                ("users", "payload", "json", "YES", None),
            ],
            constraints=[("users", "PRIMARY KEY", "PRIMARY", "id")],
        )

    def model(self):
        return make_model(
            "MysqlUser",
            "users",
            {
                "id": Integer(primary_key=True, autoincrement=True),
                "email": String(120, nullable=False),
                "active": Boolean(),
                "bio": Text(),
                "seen_at": Timestamp(),
                "payload": Json(),
            },
        )

    def test_a_matching_schema_reports_no_drift(self):
        diff = diff_schema(
            FakeConnection(self.catalog()), [self.model()], dialect=Dialects.MYSQL
        )
        self.assertEqual(diff.changed_columns, [])
        self.assertEqual(diff.new_columns, [])
        self.assertEqual(diff.missing_tables, [])

    def test_a_narrower_string_reports_drift(self):
        cursor = self.catalog()
        cursor.columns[1] = ("users", "email", "varchar(60)", "NO", None)
        diff = diff_schema(
            FakeConnection(cursor), [self.model()], dialect=Dialects.MYSQL
        )
        self.assertEqual(len(diff.changed_columns), 1)
        self.assertEqual(diff.changed_columns[0][1], "email")

    def test_a_boolean_declared_over_an_integer_reports_drift(self):
        cursor = self.catalog()
        cursor.columns[2] = ("users", "active", "int", "YES", None)
        diff = diff_schema(
            FakeConnection(cursor), [self.model()], dialect=Dialects.MYSQL
        )
        self.assertEqual([c[1] for c in diff.changed_columns], ["active"])

    def test_a_new_column_generates_mysql_ddl(self):
        cursor = self.catalog()
        del cursor.columns[4]
        migration = autogenerate(
            FakeConnection(cursor),
            [self.model()],
            id="add_seen_at",
            dialect=Dialects.MYSQL,
        )
        self.assertEqual(
            migration.up, ["ALTER TABLE `users` ADD COLUMN `seen_at` DATETIME"]
        )

    def test_a_missing_table_generates_mysql_create(self):
        migration = autogenerate(
            FakeConnection(FakeCursor()),
            [self.model()],
            id="create",
            dialect=Dialects.MYSQL,
        )
        self.assertEqual(
            migration.up,
            [
                "CREATE TABLE `users` ("
                "`id` INT AUTO_INCREMENT PRIMARY KEY, "
                "`email` VARCHAR(120) NOT NULL, "
                "`active` TINYINT(1), "
                "`bio` TEXT, "
                "`seen_at` DATETIME, "
                "`payload` JSON)"
            ],
        )
        self.assertEqual(migration.down, ["DROP TABLE IF EXISTS `users`"])

    def test_a_reference_becomes_its_own_statement(self):
        # InnoDB parses a column-level REFERENCES clause and creates
        # nothing, so the foreign key has to be its own statement. It
        # runs after every new table exists, so the target need not be
        # created first.
        model = make_model(
            "MysqlShow",
            "shows",
            {
                "id": Integer(primary_key=True, autoincrement=True),
                "venue_id": Integer(references="venues.id"),
            },
        )
        migration = autogenerate(
            FakeConnection(FakeCursor()), [model], id="create", dialect=Dialects.MYSQL
        )
        self.assertEqual(
            migration.up,
            [
                "CREATE TABLE `shows` ("
                "`id` INT AUTO_INCREMENT PRIMARY KEY, "
                "`venue_id` INT)",
                "ALTER TABLE `shows` ADD CONSTRAINT `fk_shows_venue_id` "
                "FOREIGN KEY (`venue_id`) REFERENCES `venues` (`id`)",
            ],
        )
        self.assertEqual(
            migration.down,
            [
                "ALTER TABLE `shows` DROP FOREIGN KEY `fk_shows_venue_id`",
                "DROP TABLE IF EXISTS `shows`",
            ],
        )

    def test_an_added_reference_becomes_its_own_statement(self):
        cursor = FakeCursor(columns=[("shows", "id", "int", "NO", None)])
        model = make_model(
            "MysqlShowAdd",
            "shows",
            {
                "id": Integer(primary_key=True),
                "venue_id": Integer(references="venues.id"),
            },
        )
        migration = autogenerate(
            FakeConnection(cursor), [model], id="add_fk", dialect=Dialects.MYSQL
        )
        self.assertEqual(
            migration.up,
            [
                "ALTER TABLE `shows` ADD COLUMN `venue_id` INT",
                "ALTER TABLE `shows` ADD CONSTRAINT `fk_shows_venue_id` "
                "FOREIGN KEY (`venue_id`) REFERENCES `venues` (`id`)",
            ],
        )
        # The key goes before the column, or MySQL refuses the drop.
        self.assertEqual(
            migration.down,
            [
                "ALTER TABLE `shows` DROP FOREIGN KEY `fk_shows_venue_id`",
                "ALTER TABLE `shows` DROP COLUMN `venue_id`",
            ],
        )

    def test_a_backfilled_reference_adds_the_key_after_tightening(self):
        cursor = FakeCursor(columns=[("shows", "id", "int", "NO", None)])
        model = make_model(
            "MysqlShowFill",
            "shows",
            {
                "id": Integer(primary_key=True),
                "venue_id": Integer(references="venues.id", nullable=False, backfill=1),
            },
        )
        migration = autogenerate(
            FakeConnection(cursor), [model], id="fill_fk", dialect=Dialects.MYSQL
        )
        self.assertEqual(
            migration.up,
            [
                "ALTER TABLE `shows` ADD COLUMN `venue_id` INT",
                "UPDATE `shows` SET `venue_id` = 1 WHERE `venue_id` IS NULL",
                "ALTER TABLE `shows` MODIFY COLUMN `venue_id` INT NOT NULL",
                "ALTER TABLE `shows` ADD CONSTRAINT `fk_shows_venue_id` "
                "FOREIGN KEY (`venue_id`) REFERENCES `venues` (`id`)",
            ],
        )

    def test_a_type_change_generates_modify_column(self):
        cursor = self.catalog()
        cursor.columns[0] = ("users", "id", "bigint", "NO", None)
        model = make_model(
            "MysqlSmall", "users", {"id": Integer(primary_key=True), "bio": Text()}
        )
        cursor.columns = [cursor.columns[0], ("users", "bio", "text", "YES", None)]
        migration = autogenerate(
            FakeConnection(cursor), [model], id="shrink", dialect=Dialects.MYSQL
        )
        self.assertEqual(
            migration.up, ["ALTER TABLE `users` MODIFY COLUMN `id` INT NOT NULL"]
        )
        self.assertEqual(
            migration.down, ["ALTER TABLE `users` MODIFY COLUMN `id` bigint NOT NULL"]
        )

    def test_a_type_change_keeps_the_default_and_the_comment(self):
        cursor = self.catalog()
        cursor.columns = [
            ("users", "id", "int", "NO", None),
            ("users", "rank", "int", "NO", None),
        ]
        cursor.comments = [("users", "rank", "seat order")]
        model = make_model(
            "MysqlRanked",
            "users",
            {
                "id": Integer(primary_key=True),
                "rank": BigInteger(nullable=False, default=1, comment="seat order"),
            },
        )
        migration = autogenerate(
            FakeConnection(cursor), [model], id="widen", dialect=Dialects.MYSQL
        )
        self.assertEqual(
            migration.up,
            [
                "ALTER TABLE `users` MODIFY COLUMN `rank` BIGINT NOT NULL "
                "DEFAULT 1 COMMENT 'seat order'"
            ],
        )
        self.assertEqual(
            migration.down,
            [
                "ALTER TABLE `users` MODIFY COLUMN `rank` int NOT NULL "
                "DEFAULT 1 COMMENT 'seat order'"
            ],
        )


class TestMariadbPrecisionDrift(unittest.TestCase):
    """
    MariaDB reports a datetime(6) column with its precision, and the
    model's DATETIME carries none. The two must not read as a type
    change: rewriting the column would truncate it to whole seconds.
    """

    def model(self, nullable):
        return make_model(
            "MariaEvent",
            "events",
            {
                "id": Integer(primary_key=True, autoincrement=True),
                "seen_at": Timestamp(nullable=nullable),
            },
        )

    def catalog(self, nullable):
        return FakeCursor(
            columns=[
                ("events", "id", "int", "NO", None),
                ("events", "seen_at", "datetime(6)", "YES" if nullable else "NO", None),
            ],
            constraints=[("events", "PRIMARY KEY", "PRIMARY", "id")],
        )

    def test_precision_alone_is_not_drift(self):
        diff = diff_schema(
            FakeConnection(self.catalog(True)),
            [self.model(True)],
            dialect=Dialects.MYSQL,
        )
        self.assertEqual(diff.changed_columns, [])

    def test_a_nullability_change_does_not_rewrite_the_type(self):
        migration = autogenerate(
            FakeConnection(self.catalog(False)),
            [self.model(True)],
            id="relax",
            dialect=Dialects.MYSQL,
        )
        self.assertEqual(
            migration.up, ["ALTER TABLE `events` MODIFY COLUMN `seen_at` DATETIME"]
        )

    def test_a_real_parameter_change_is_still_drift(self):
        cursor = self.catalog(True)
        cursor.columns[1] = ("events", "seen_at", "varchar(80)", "YES", None)
        model = make_model(
            "MariaEventText",
            "events",
            {
                "id": Integer(primary_key=True, autoincrement=True),
                "seen_at": String(120),
            },
        )
        diff = diff_schema(FakeConnection(cursor), [model], dialect=Dialects.MYSQL)
        self.assertEqual([c[1] for c in diff.changed_columns], ["seen_at"])


class TestMysqlSchemaScope(unittest.TestCase):
    """
    Every MySQL query covers the same schemas. A read that took its
    columns from one schema and its indexes from another would attach an
    index to a table of the same name in another database.
    """

    def reads(self, schemas=()):
        cursor = FakeCursor(
            columns=[("events", "id", "int", "NO", None)],
            constraints=[],
            checks=[],
            table_checks=[],
        )
        introspect_schema(FakeConnection(cursor), Dialects.MYSQL, schemas)
        return [s for s in cursor.statements if "information_schema" in s]

    def test_every_read_covers_the_connection_database(self):
        reads = self.reads()
        self.assertTrue(reads)
        for sql in reads:
            self.assertIn("= DATABASE()", sql)

    def test_a_declared_schema_does_not_drop_the_connection_database(self):
        reads = self.reads(("reporting",))
        self.assertTrue(reads)
        for sql in reads:
            self.assertIn("= DATABASE()", sql)
            self.assertIn("IN ('reporting')", sql)

    def test_the_index_and_check_reads_carry_the_declared_schema(self):
        reads = self.reads(("reporting",))
        statistics = [s for s in reads if "statistics" in s]
        recovery = [s for s in reads if s.startswith("SELECT table_name, check_clause")]
        self.assertEqual(len(statistics), 1)
        self.assertEqual(len(recovery), 1)
        self.assertIn("table_schema IN ('reporting')", statistics[0])
        self.assertIn("constraint_schema IN ('reporting')", recovery[0])


class TestTwoSchemasOneTableName(unittest.TestCase):
    """
    A snapshot keys on the bare table name. A declared tableSchema widens
    the read to two schemas, and an undeclared table of the same name in
    the other schema would merge its columns into the declared one.
    """

    def read(self, columns, commented=None, schemas=("app",)):
        cursor = FakeCursor(
            columns=columns,
            commented_columns=commented,
            constraints=[],
            checks=[],
            table_checks=[],
        )
        return introspect_schema(FakeConnection(cursor), Dialects.MYSQL, schemas)

    def test_one_table_name_in_two_schemas_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.read(
                [
                    ("users", "id", "int", "NO", None, None, "app"),
                    ("users", "email", "varchar(120)", "YES", None, None, "public"),
                ],
                commented=[
                    ("users", "id", "int", "NO", None, None, "app"),
                    ("users", "email", "varchar(120)", "YES", None, None, "public"),
                ],
            )
        message = str(caught.exception)
        self.assertIn("app", message)
        self.assertIn("public", message)
        self.assertIn("users", message)

    def test_one_schema_reads_the_table(self):
        schema = self.read(
            [
                ("users", "id", "int", "NO", None, "hello", "app"),
                ("users", "email", "varchar(120)", "YES", None, None, "app"),
            ],
            commented=[
                ("users", "id", "int", "NO", None, "hello", "app"),
                ("users", "email", "varchar(120)", "YES", None, None, "app"),
            ],
        )
        self.assertEqual(sorted(schema["users"].columns), ["email", "id"])
        self.assertEqual(schema["users"].columns["id"].comment, "hello")

    def test_a_read_without_the_comment_column_reads_the_schema(self):
        # The comment read failed, so the schema name arrives one column
        # earlier and must still be read as a schema name.
        with self.assertRaises(ValueError):
            self.read(
                [
                    ("users", "id", "int", "NO", None, "app"),
                    ("users", "email", "varchar(120)", "YES", None, "public"),
                ]
            )


if __name__ == "__main__":
    unittest.main()
