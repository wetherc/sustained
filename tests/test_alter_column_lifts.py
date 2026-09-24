"""
Indexes and defaults that stop an ALTER COLUMN statement. DuckDB refuses
to change any column of a table that has an index, SQL Server refuses
to change a column in an index or a UNIQUE constraint, and SQL Server
refuses to change the type of a column that has a default. A generated
migration takes them off before the change and puts them back after it,
on the way up and on the way down.
"""

import unittest
from unittest import mock

from sustained import create_model
from sustained.autogenerate import autogenerate
from sustained.dialects import Dialects
from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
)
from sustained.schema import BigInteger, Index, Integer


def widgets(size=BigInteger, count=Integer, **extra):
    model = create_model("LiftWidgets", "widgets")
    model.tableColumns = {
        "id": Integer(primary_key=True),
        "size": size(nullable=True, default=5),
        "count": count(nullable=True),
        "code": Integer(nullable=True, unique=True),
        **extra,
    }
    model.columns = tuple(model.tableColumns)
    model.indexes = [Index("ix_size", "size"), Index("ix_count", "count")]
    return model


def snapshot(default="((5))"):
    return Snapshot(
        tables={
            "widgets": IntrospectedTable(
                columns={
                    "id": IntrospectedColumn("int", False, True, name="id"),
                    "size": IntrospectedColumn(
                        "int", True, False, default=default, name="size"
                    ),
                    "count": IntrospectedColumn("int", True, False, name="count"),
                    "code": IntrospectedColumn("int", True, False, name="code"),
                },
                primary_key=("id",),
                indexes={
                    "ix_size": IntrospectedIndex(("size",), False, name="ix_size"),
                    "ix_count": IntrospectedIndex(("count",), False, name="ix_count"),
                    "uq_code": IntrospectedIndex(
                        ("code",), True, constraint=True, name="uq_code"
                    ),
                },
                name="widgets",
            )
        },
        constraints_read=True,
        checks_read=True,
    )


def generate(model, dialect, schema=None):
    with mock.patch(
        "sustained.autogenerate.introspect_schema",
        return_value=schema or snapshot(),
    ):
        return autogenerate(None, [model], id="m", dialect=dialect)


class TestDuckdbLiftsEveryIndexOnTheTable(unittest.TestCase):
    def test_a_type_change_drops_and_creates_the_indexes(self):
        migration = generate(widgets(), Dialects.DUCKDB, snapshot(default="5"))
        self.assertEqual(
            migration.up,
            [
                'DROP INDEX "ix_size"',
                'DROP INDEX "ix_count"',
                'ALTER TABLE "widgets" ALTER COLUMN "size" SET DATA TYPE BIGINT',
                'CREATE INDEX "ix_size" ON "widgets" ("size")',
                'CREATE INDEX "ix_count" ON "widgets" ("count")',
            ],
        )
        self.assertEqual(
            migration.down,
            [
                'DROP INDEX "ix_size"',
                'DROP INDEX "ix_count"',
                'ALTER TABLE "widgets" ALTER COLUMN "size" SET DATA TYPE int',
                'CREATE INDEX "ix_size" ON "widgets" ("size")',
                'CREATE INDEX "ix_count" ON "widgets" ("count")',
            ],
        )
        # A dropped index stops the change until the transaction commits.
        self.assertFalse(migration.transactional)

    def test_a_new_not_null_column_lifts_the_table(self):
        model = widgets(size=Integer, added=Integer(nullable=False, backfill=0))
        migration = generate(model, Dialects.DUCKDB, snapshot(default="5"))
        self.assertEqual(
            migration.up[:2], ['DROP INDEX "ix_size"', 'DROP INDEX "ix_count"']
        )
        self.assertEqual(
            migration.up[-2:],
            [
                'CREATE INDEX "ix_size" ON "widgets" ("size")',
                'CREATE INDEX "ix_count" ON "widgets" ("count")',
            ],
        )

    def test_ignored_column_changes_lift_nothing(self):
        model = widgets(added=Integer(nullable=True))
        with mock.patch(
            "sustained.autogenerate.introspect_schema",
            return_value=snapshot(default="5"),
        ):
            migration = autogenerate(
                None,
                [model],
                id="m",
                dialect=Dialects.DUCKDB,
                ignore_changed_columns=True,
            )
        self.assertEqual(
            migration.up, ['ALTER TABLE "widgets" ADD COLUMN "added" INTEGER']
        )
        self.assertTrue(migration.transactional)


class TestMssqlLiftsWhatDependsOnTheColumn(unittest.TestCase):
    def test_the_index_and_the_default_come_off_a_type_change(self):
        migration = generate(widgets(), Dialects.MSSQL)
        drop_default = migration.up[1]
        self.assertTrue(drop_default.startswith("DECLARE @sustained_default"))
        self.assertIn("OBJECT_ID(N'[widgets]')", drop_default)
        self.assertIn("c.name = N'size'", drop_default)
        self.assertEqual(
            [migration.up[0]] + migration.up[2:],
            [
                "DROP INDEX [ix_size] ON [widgets]",
                "ALTER TABLE [widgets] ALTER COLUMN [size] BIGINT NULL",
                "ALTER TABLE [widgets] ADD DEFAULT ((5)) FOR [size]",
                "CREATE INDEX [ix_size] ON [widgets] ([size])",
            ],
        )
        self.assertEqual(
            migration.down,
            [
                "DROP INDEX [ix_size] ON [widgets]",
                drop_default,
                "ALTER TABLE [widgets] ALTER COLUMN [size] int NULL",
                "ALTER TABLE [widgets] ADD DEFAULT ((5)) FOR [size]",
                "CREATE INDEX [ix_size] ON [widgets] ([size])",
            ],
        )
        self.assertTrue(migration.transactional)

    def test_a_unique_constraint_comes_off_and_back(self):
        model = widgets(size=Integer)
        model.tableColumns["code"] = BigInteger(nullable=True, unique=True)
        migration = generate(model, Dialects.MSSQL)
        self.assertEqual(
            migration.up,
            [
                "ALTER TABLE [widgets] DROP CONSTRAINT [uq_code]",
                "ALTER TABLE [widgets] ALTER COLUMN [code] BIGINT NULL",
                "ALTER TABLE [widgets] ADD CONSTRAINT [uq_code] UNIQUE ([code])",
            ],
        )

    def test_a_nullability_change_keeps_the_default(self):
        migration = generate(
            widgets(size=lambda **kw: Integer(**{**kw, "nullable": False})),
            Dialects.MSSQL,
        )
        self.assertEqual(
            migration.up,
            [
                "DROP INDEX [ix_size] ON [widgets]",
                "UPDATE [widgets] SET [size] = 5 WHERE [size] IS NULL",
                "ALTER TABLE [widgets] ALTER COLUMN [size] INTEGER NOT NULL",
                "CREATE INDEX [ix_size] ON [widgets] ([size])",
            ],
        )

    def test_a_column_without_a_default_takes_no_default_statement(self):
        migration = generate(widgets(), Dialects.MSSQL, snapshot(default=None))
        self.assertFalse(any("DEFAULT" in statement for statement in migration.up))


class TestMssqlRestatesTheCollation(unittest.TestCase):
    def test_a_text_column_keeps_its_collation(self):
        from sustained.schema import String

        schema = snapshot(default=None)
        columns = schema["widgets"].columns
        columns["code"] = columns["code"]._replace(
            raw_type="varchar(10)", collation="Latin1_General_BIN"
        )
        model = widgets(size=Integer)
        model.tableColumns["code"] = String(20, unique=True)
        migration = generate(model, Dialects.MSSQL, schema)
        self.assertIn(
            "ALTER TABLE [widgets] ALTER COLUMN [code] NVARCHAR(20) "
            "COLLATE Latin1_General_BIN NULL",
            migration.up,
        )
        self.assertIn(
            "ALTER TABLE [widgets] ALTER COLUMN [code] varchar(10) "
            "COLLATE Latin1_General_BIN NULL",
            migration.down,
        )

    def test_another_type_takes_no_collation(self):
        schema = snapshot(default=None)
        columns = schema["widgets"].columns
        columns["size"] = columns["size"]._replace(collation="Latin1_General_BIN")
        migration = generate(widgets(), Dialects.MSSQL, schema)
        self.assertIn(
            "ALTER TABLE [widgets] ALTER COLUMN [size] BIGINT NULL", migration.up
        )

    def test_the_read_takes_the_collation_after_the_schema(self):
        from sustained.introspect import MSSQL_CATALOG, _information_schema_plan

        plan = _information_schema_plan(MSSQL_CATALOG)
        self.assertIn("c.table_schema, c.collation_name, ", next(plan))
        try:
            plan.send(
                [
                    ("t", "code", "varchar", "YES", None, "dbo", "Latin1_General_BIN"),
                    ("t", "n", "int", "YES", None, "dbo", None),
                ]
            )
            while True:
                plan.send([])
        except StopIteration as stop:
            columns = stop.value["t"].columns
        self.assertEqual(columns["code"].collation, "Latin1_General_BIN")
        self.assertIsNone(columns["n"].collation)


class TestPostgresLiftsNothing(unittest.TestCase):
    def test_the_engine_rebuilds_its_own_indexes(self):
        migration = generate(widgets(), Dialects.POSTGRES, snapshot(default="5"))
        self.assertEqual(
            migration.up,
            ['ALTER TABLE "widgets" ALTER COLUMN "size" TYPE BIGINT'],
        )


class TestDefaultStatements(unittest.TestCase):
    def test_the_ansi_spelling(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        self.assertEqual(
            compiler.compile_drop_column_default('"t"', "c"),
            'ALTER TABLE "t" ALTER COLUMN "c" DROP DEFAULT',
        )
        self.assertEqual(
            compiler.compile_add_column_default('"t"', "c", "5"),
            'ALTER TABLE "t" ALTER COLUMN "c" SET DEFAULT 5',
        )

    def test_the_sql_server_drop_hides_its_drop_in_a_literal(self):
        from sustained.analysis import destructive_statements

        compiler = Dialects.get_compiler(Dialects.MSSQL)
        statement = compiler.compile_drop_column_default("[a'b]", "c")
        self.assertIn("N'ALTER TABLE [a''b] DROP CONSTRAINT '", statement)
        self.assertEqual(destructive_statements(statement), [])


if __name__ == "__main__":
    unittest.main()
