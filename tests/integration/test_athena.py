"""
AWS Athena: queries only, against your own account.

There is no container for Athena, so this needs credentials you supply: a
named profile in AWS_PROFILE, or static keys in the usual AWS variables,
plus SUSTAINED_TEST_ATHENA_S3_DIR pointing at a staging directory the
account can write to. The query reads information_schema, so it creates
nothing and scans no data.
"""

import unittest

from sustained.dialects import Dialects
from sustained.model import Model

from . import harness


class AthenaQueries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.connection = harness.connect("athena")

    @classmethod
    def tearDownClass(cls):
        connection = getattr(cls, "connection", None)
        if connection is not None:
            connection.close()

    def setUp(self):
        self.Catalog = type("Catalog", (Model,), {"_dialect": Dialects.ATHENA})
        self.Catalog.bind(self.connection)

    def tearDown(self):
        self.Catalog.unbind()

    def test_a_parameterized_select_reaches_the_account(self):
        rows = (
            self.Catalog.query()
            .select("table_name")
            .from_("information_schema.tables")
            .where("table_schema", "=", "information_schema")
            .limit(5)
            .run()
        )
        self.assertTrue(rows)
        self.assertTrue(all(row.table_name for row in rows))
