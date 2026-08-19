"""
AWS Athena: queries only, against your own account.

There is no container for Athena, so this needs credentials you supply: a
named profile in AWS_PROFILE, or static keys in the usual AWS variables,
plus SUSTAINED_TEST_ATHENA_S3_DIR pointing at a staging directory the
account can write to. The probe reads information_schema, so it creates
nothing and scans no data.
"""

from sustained.dialects import Dialects

from . import queries


class AthenaQueries(queries.QueriesCase):
    NAME = "athena"
    DIALECT = Dialects.ATHENA
    READ_ONLY = True
    SOURCE = "information_schema.tables"
    PROBE = ("table_name", "table_schema", "information_schema")
    EXPECTED = None

    def test_a_string_function_and_limit_run_on_the_catalog(self):
        rows = (
            self.Reader.query()
            .select("table_name")
            .select_func("UPPER", "table_name", alias="loud_name")
            .from_(self.SOURCE)
            .where("table_schema", "=", "information_schema")
            .limit(3)
            .run()
        )
        self.assertTrue(rows)
        self.assertLessEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row.table_name.upper(), row.loud_name)
