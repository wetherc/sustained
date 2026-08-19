"""
The covers contract.

support.json carries a `covers` list per server, and the support page
prints it. This test makes each name in that list a proven claim: every
cover maps to exactly one test mixin here, and every server's test module
mixes in exactly the mixins its row names. A cover with no mixin, or a
mixin no server claims, fails the suite. This needs no server, so it runs
on every plain test run, not only under matrix.py.
"""

import importlib
import unittest

from . import harness, lifecycle, queries, writes

COVERS = {
    "queries": queries.QueriesCase,
    "writes": writes.WritesCase,
    "migrations": lifecycle.ServerCase,
}


def rows():
    """The support.json rows that have a test module."""
    return [row for row in harness.SUPPORT["databases"] if row["server"] != "none"]


def classes_of(name):
    """The test classes a server's module defines itself."""
    module = importlib.import_module(f".test_{name}", package=__package__)
    return [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, unittest.TestCase)
        and value.__module__ == module.__name__
    ]


class CoversContract(unittest.TestCase):
    def test_every_cover_name_has_a_mixin(self):
        for row in rows():
            with self.subTest(server=row["name"]):
                self.assertLessEqual(set(row["covers"]), set(COVERS))

    def test_every_mixin_is_claimed_by_a_server(self):
        claimed = set()
        for row in rows():
            claimed.update(row["covers"])
        self.assertEqual(set(COVERS), claimed)

    def test_each_server_mixes_in_exactly_what_its_row_claims(self):
        for row in rows():
            with self.subTest(server=row["name"]):
                classes = classes_of(row["name"])
                mixed = {
                    cover
                    for cover, mixin in COVERS.items()
                    if any(issubclass(cls, mixin) for cls in classes)
                }
                self.assertEqual(set(row["covers"]), mixed)

    def test_each_test_class_names_its_own_server(self):
        for row in rows():
            for cls in classes_of(row["name"]):
                with self.subTest(server=row["name"], cls=cls.__name__):
                    self.assertEqual(row["name"], cls.NAME)
