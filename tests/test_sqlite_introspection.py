"""
Tests for reading a SQLite schema's constraints. SQLite has no catalog
view for constraint names or check expressions, so the reader combines
the PRAGMA tables with the CREATE TABLE statements in sqlite_master.
"""

import sqlite3
import unittest

from sustained.introspect import introspect_schema


class TestSqliteForeignKeys(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE venues (id INTEGER PRIMARY KEY, city TEXT)")

    def tearDown(self):
        self.conn.close()

    def test_a_named_constraint_keeps_its_name_and_actions(self):
        self.conn.execute(
            "CREATE TABLE shows (id INTEGER PRIMARY KEY, venue_id INTEGER, "
            "CONSTRAINT fk_shows_venue FOREIGN KEY (venue_id) "
            "REFERENCES venues (id) ON DELETE CASCADE ON UPDATE SET NULL)"
        )
        schema = introspect_schema(self.conn)
        fk = schema["shows"].foreign_keys["fk_shows_venue"]
        self.assertEqual(fk.columns, ("venue_id",))
        self.assertEqual(fk.target_table, "venues")
        self.assertEqual(fk.target_columns, ("id",))
        self.assertEqual(fk.on_delete, "CASCADE")
        self.assertEqual(fk.on_update, "SET NULL")

    def test_an_unnamed_key_gets_a_stable_synthetic_name(self):
        self.conn.execute(
            "CREATE TABLE shows (id INTEGER PRIMARY KEY, "
            "venue_id INTEGER REFERENCES venues (id))"
        )
        schema = introspect_schema(self.conn)
        fk = schema["shows"].foreign_keys["fk_shows_0"]
        self.assertEqual(fk.columns, ("venue_id",))
        self.assertEqual(fk.target_table, "venues")

    def test_a_composite_key_keeps_its_column_order(self):
        self.conn.execute(
            "CREATE TABLE seats (venue_id INTEGER, row_no INTEGER, "
            "CONSTRAINT fk_seats_spot FOREIGN KEY (venue_id, row_no) "
            "REFERENCES spots (venue_id, row_no))"
        )
        schema = introspect_schema(self.conn)
        fk = schema["seats"].foreign_keys["fk_seats_spot"]
        self.assertEqual(fk.columns, ("venue_id", "row_no"))
        self.assertEqual(fk.target_columns, ("venue_id", "row_no"))

    def test_a_reference_to_an_implicit_key_maps_to_the_table(self):
        self.conn.execute(
            "CREATE TABLE shows (id INTEGER PRIMARY KEY, "
            "venue_id INTEGER REFERENCES venues)"
        )
        schema = introspect_schema(self.conn)
        fk = schema["shows"].foreign_keys["fk_shows_0"]
        self.assertEqual(fk.target_columns, ())
        self.assertEqual(schema["shows"].foreign_key_targets, {"venue_id": "venues"})

    def test_the_old_column_to_target_mapping_still_reads(self):
        self.conn.execute(
            "CREATE TABLE shows (id INTEGER PRIMARY KEY, "
            "venue_id INTEGER REFERENCES venues (id))"
        )
        schema = introspect_schema(self.conn)
        self.assertEqual(schema["shows"].foreign_key_targets, {"venue_id": "venues.id"})


class TestSqliteChecks(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def read(self, create_sql):
        self.conn.execute(create_sql)
        return introspect_schema(self.conn)

    def test_a_ck_named_check_is_read_back(self):
        schema = self.read(
            "CREATE TABLE shows (seats INTEGER, "
            "CONSTRAINT ck_shows_seats CHECK (seats > 0))"
        )
        self.assertEqual(schema["shows"].checks, {"ck_shows_seats": "seats > 0"})

    def test_nested_parentheses_stay_balanced(self):
        schema = self.read(
            "CREATE TABLE posts (status TEXT, CONSTRAINT ck_posts_status_enum "
            "CHECK (status IN ('draft', 'published')))"
        )
        self.assertEqual(
            schema["posts"].checks,
            {"ck_posts_status_enum": "status IN ('draft', 'published')"},
        )

    def test_a_quoted_close_paren_does_not_end_the_expression(self):
        schema = self.read(
            "CREATE TABLE posts (tag TEXT, "
            "CONSTRAINT ck_posts_tag CHECK (tag <> ')'))"
        )
        self.assertEqual(schema["posts"].checks, {"ck_posts_tag": "tag <> ')'"})

    def test_a_check_without_the_ck_prefix_is_read_back(self):
        schema = self.read(
            "CREATE TABLE shows (seats INTEGER, "
            "CONSTRAINT positive_seats CHECK (seats > 0))"
        )
        self.assertEqual(dict(schema["shows"].checks), {"positive_seats": "seats > 0"})

    def test_a_quoted_constraint_name_is_read_back(self):
        schema = self.read(
            'CREATE TABLE shows (seats INTEGER, CONSTRAINT "seat_floor" '
            "CHECK (seats >= 0))"
        )
        self.assertEqual(dict(schema["shows"].checks), {"seat_floor": "seats >= 0"})

    def test_an_unnamed_check_stays_unread(self):
        schema = self.read("CREATE TABLE shows (seats INTEGER CHECK (seats > 0))")
        self.assertEqual(dict(schema["shows"].checks), {})

    def test_unbalanced_parentheses_read_as_no_check(self):
        from sustained.introspect import _sqlite_table_checks

        truncated = "CREATE TABLE x (a INTEGER, CONSTRAINT ck_x CHECK (a > (b"
        self.assertEqual(_sqlite_table_checks(truncated), {})

    def test_a_table_without_checks_reports_none(self):
        schema = self.read("CREATE TABLE plain (id INTEGER PRIMARY KEY)")
        self.assertEqual(dict(schema["plain"].checks), {})
        self.assertEqual(schema.enum_types, {})


if __name__ == "__main__":
    unittest.main()
