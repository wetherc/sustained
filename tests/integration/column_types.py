"""
Column type fidelity, part of the `migrations` cover.

One widget carries a date, a timestamp, an exact decimal, a boolean, a
byte string, a server default, a unique constraint, and a plain index.
Three claims are proven per server: the created schema does not drift
against its own DDL, inserted values read back as the values they were,
and an up/down/up cycle rebuilds a schema that introspects the same.

ServerCase in lifecycle.py mixes this in, so every server that claims
the `migrations` cover runs these tests.
"""

import datetime
import decimal

from sustained.introspect import diff_snapshots
from sustained.model import Model
from sustained.schema import (
    Binary,
    Boolean,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    Timestamp,
)


def typed_columns():
    return {
        "id": Integer(primary_key=True),
        "name": String(80, nullable=False, unique=True),
        "grade": String(12, nullable=False, default="raw"),
        "born": Date(),
        "seen": Timestamp(),
        "price": Numeric(10, 2),
        "active": Boolean(),
        "payload": Binary(),
    }


# Dates, timestamps, and decimals go in as ISO strings: every server casts
# them on insert, and no driver needs its own adapter for a plain string.
ROW = {
    "id": 1,
    "name": "hinge",
    "born": "2024-05-17",
    "seen": "2024-05-17 12:30:45",
    "price": "199.99",
    "active": True,
    "payload": b"\x00\x01\xff",
}


def as_date(value):
    """A date, whatever the driver returned it as."""
    if isinstance(value, str):
        return datetime.date.fromisoformat(value)
    if isinstance(value, datetime.datetime):
        return value.date()
    return value


def as_datetime(value):
    if isinstance(value, str):
        return datetime.datetime.fromisoformat(value)
    return value


def as_decimal(value):
    return decimal.Decimal(str(value))


class ColumnTypeTests:
    """Mixed into lifecycle.ServerCase; uses its helpers and fixtures."""

    def typed_model(self):
        return type(
            "WidgetTyped",
            (Model,),
            {
                "tableName": "it_widgets",
                "tableColumns": typed_columns(),
                "indexes": [Index("it_widgets_born_idx", "born")],
                "_dialect": self.DIALECT,
            },
        )

    def test_every_column_type_lands_and_reads_back(self):
        model = self.typed_model()
        model.bind(self.connection)
        try:
            migrator = self.migrator()
            migrator.up(models=[model])
            self.assertIsNone(migrator.plan([model]))

            model.query().insert([ROW]).run()
            row = model.query().where("id", "=", 1).orderBy("id").first()
            self.assertEqual("hinge", row.name)
            self.assertEqual("raw", row.grade)  # the server default filled it
            self.assertEqual(datetime.date(2024, 5, 17), as_date(row.born))
            self.assertEqual(
                datetime.datetime(2024, 5, 17, 12, 30, 45), as_datetime(row.seen)
            )
            self.assertEqual(decimal.Decimal("199.99"), as_decimal(row.price))
            self.assertTrue(bool(row.active))
            self.assertEqual(b"\x00\x01\xff", bytes(row.payload))
        finally:
            model.unbind()

    def test_an_up_down_up_cycle_rebuilds_the_same_schema(self):
        model = self.typed_model()
        model.bind(self.connection)
        try:
            migrator = self.migrator()
            migrator.up(models=[model])
            first = self.tables()

            migrator.down()
            self.assertNotIn("it_widgets", self.tables())

            migrator.up(models=[model])
            self.assertEqual([], diff_snapshots(first, self.tables()))
        finally:
            model.unbind()
