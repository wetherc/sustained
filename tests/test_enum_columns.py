"""
Tests for enum columns: the Enum factory, per-dialect rendering, the
enum type compiler methods, statement ordering around CREATE TABLE and
DROP TABLE, autogeneration of shared types, and the DROP TYPE guard.
"""

import enum
import sqlite3
import unittest
from unittest import mock

from sustained import DialectError, Model
from sustained.analysis import destructive_statements
from sustained.autogenerate import SchemaDiff, autogenerate
from sustained.dialects import Dialects
from sustained.guards import no_drops
from sustained.introspect import Snapshot
from sustained.migrations import create_table_migration
from sustained.schema import Enum, Integer, String, collect_enum_types
from sustained.types import Expression

try:
    import duckdb

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False


class Status(enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"


class EnumPost(Model):
    tableName = "posts"
    tableColumns = {
        "id": Integer(primary_key=True),
        "status": Enum(
            "draft", "published", name="post_status", nullable=False, default="draft"
        ),
    }


class TestEnumFactory(unittest.TestCase):
    def test_values_and_name_recorded(self):
        col = Enum("a", "b", name="ab")
        self.assertEqual(col.type_name, "ENUM")
        self.assertEqual(col.enum_name, "ab")
        self.assertEqual(col.enum_values, ("a", "b"))

    def test_enum_class_values(self):
        col = Enum(Status, name="post_status")
        self.assertEqual(col.enum_values, ("draft", "published"))

    def test_enum_class_with_non_string_values_rejected(self):
        class Bad(enum.Enum):
            ONE = 1

        with self.assertRaisesRegex(ValueError, "not a string"):
            Enum(Bad, name="bad")

    def test_name_required(self):
        with self.assertRaises(TypeError):
            Enum("a", "b")
        with self.assertRaisesRegex(ValueError, "needs a name"):
            Enum("a", "b", name="")

    def test_no_values_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one value"):
            Enum(name="empty")

    def test_non_string_value_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be strings"):
            Enum("a", 2, name="mixed")

    def test_duplicate_values_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            Enum("a", "a", name="dup")

    def test_default_must_be_a_value(self):
        with self.assertRaisesRegex(ValueError, "not a value of enum"):
            Enum("a", "b", name="ab", default="c")

    def test_expression_default_passes(self):
        col = Enum("a", "b", name="ab", default=Expression("'a'"))
        self.assertEqual(str(col.default), "'a'")

    def test_enum_arguments_rejected_on_other_types(self):
        with self.assertRaisesRegex(ValueError, "only valid on an ENUM"):
            Integer(enum_name="nope")


class TestCollectEnumTypes(unittest.TestCase):
    def test_shared_type_collected_once(self):
        columns = {
            "a": Enum("x", "y", name="t"),
            "b": Enum("x", "y", name="t"),
        }
        self.assertEqual(collect_enum_types(columns), {"t": ("x", "y")})

    def test_conflicting_values_rejected(self):
        columns = {
            "a": Enum("x", name="t"),
            "b": Enum("y", name="t"),
        }
        with self.assertRaisesRegex(ValueError, "different\\s+values"):
            collect_enum_types(columns)


class TestEnumRendering(unittest.TestCase):
    def tearDown(self):
        EnumPost.set_dialect(Dialects.DEFAULT)

    def test_postgres_creates_the_type_first(self):
        EnumPost.set_dialect(Dialects.POSTGRES)
        statements = EnumPost.create_table_statements()
        self.assertEqual(
            statements[0],
            "CREATE TYPE \"post_status\" AS ENUM ('draft', 'published')",
        )
        self.assertIn('"status" "post_status" NOT NULL', statements[1])

    def test_postgres_drop_type_follows_drop_table(self):
        EnumPost.set_dialect(Dialects.POSTGRES)
        statements = EnumPost.drop_table_statements()
        self.assertEqual(
            statements,
            ['DROP TABLE IF EXISTS "posts"', 'DROP TYPE IF EXISTS "post_status"'],
        )

    def test_mysql_renders_inline(self):
        EnumPost.set_dialect(Dialects.MYSQL)
        statements = EnumPost.create_table_statements()
        self.assertEqual(len(statements), 1)
        self.assertIn("`status` ENUM('draft', 'published') NOT NULL", statements[0])
        self.assertEqual(
            EnumPost.drop_table_statements(), ["DROP TABLE IF EXISTS `posts`"]
        )

    def test_mssql_renders_varchar_with_check(self):
        EnumPost.set_dialect(Dialects.MSSQL)
        sql = EnumPost.create_table_sql()
        self.assertIn("[status] NVARCHAR(9) NOT NULL", sql)
        self.assertIn(
            "CONSTRAINT [ck_posts_status_enum] CHECK ([status] IN ('draft', 'published'))",
            sql,
        )

    def test_default_dialect_renders_varchar_with_check(self):
        sql = EnumPost.create_table_sql()
        self.assertIn("status VARCHAR(9) NOT NULL", sql)
        self.assertIn(
            "CONSTRAINT ck_posts_status_enum CHECK (status IN ('draft', 'published'))",
            sql,
        )

    def test_duckdb_creates_the_type_first(self):
        EnumPost.set_dialect(Dialects.DUCKDB)
        statements = EnumPost.create_table_statements()
        self.assertEqual(
            statements[0],
            "CREATE TYPE \"post_status\" AS ENUM ('draft', 'published')",
        )

    def test_athena_refuses(self):
        class LakeDoc(Model):
            tableName = "lake_docs"
            tableColumns = {
                "name": String(40),
                "state": Enum("new", "done", name="lake_state"),
            }
            _dialect = Dialects.ATHENA

        with self.assertRaisesRegex(DialectError, "Use String\\(\\)"):
            LakeDoc.create_table_sql()

    def test_presto_refuses(self):
        EnumPost.set_dialect(Dialects.PRESTO)
        with self.assertRaisesRegex(DialectError, "Use String\\(\\)"):
            EnumPost.create_table_sql()

    def test_create_table_migration_carries_types_both_ways(self):
        EnumPost.set_dialect(Dialects.POSTGRES)
        migration = create_table_migration(EnumPost)
        self.assertTrue(migration.up[0].startswith("CREATE TYPE"))
        self.assertTrue(migration.down[-1].startswith("DROP TYPE"))

    def test_model_enum_types_conflict_rejected(self):
        class Twice(Model):
            tableName = "twice"
            tableColumns = {
                "a": Enum("x", name="t"),
                "b": Enum("y", name="t"),
            }

        with self.assertRaisesRegex(ValueError, "different\\s+values"):
            Twice.enum_types()


class TestEnumCompilerMethods(unittest.TestCase):
    def test_base_compiler_raises(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        with self.assertRaises(DialectError):
            compiler.compile_create_enum_type("t", ["a"])
        with self.assertRaises(DialectError):
            compiler.compile_drop_enum_type("t")
        with self.assertRaises(DialectError):
            compiler.compile_add_enum_value("t", "a")

    def test_postgres_statements(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        self.assertEqual(
            compiler.compile_create_enum_type("t", ["a", "b"]),
            "CREATE TYPE \"t\" AS ENUM ('a', 'b')",
        )
        self.assertEqual(compiler.compile_drop_enum_type("t"), 'DROP TYPE "t"')
        self.assertEqual(
            compiler.compile_drop_enum_type("t", if_exists=True),
            'DROP TYPE IF EXISTS "t"',
        )
        self.assertEqual(
            compiler.compile_add_enum_value("t", "c"),
            "ALTER TYPE \"t\" ADD VALUE 'c'",
        )

    def test_duckdb_cannot_add_a_value(self):
        compiler = Dialects.get_compiler(Dialects.DUCKDB)
        self.assertEqual(
            compiler.compile_create_enum_type("t", ["a"]),
            "CREATE TYPE \"t\" AS ENUM ('a')",
        )
        with self.assertRaisesRegex(DialectError, "cannot add a value"):
            compiler.compile_add_enum_value("t", "b")

    def test_values_escape_quotes(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        self.assertEqual(
            compiler.compile_create_enum_type("t", ["it's"]),
            "CREATE TYPE \"t\" AS ENUM ('it''s')",
        )


class TestSqliteEnumEnforcement(unittest.TestCase):
    def test_check_constraint_holds_the_values(self):
        conn = sqlite3.connect(":memory:")
        cursor = conn.cursor()
        for statement in EnumPost.create_table_statements():
            cursor.execute(statement)
        cursor.execute("INSERT INTO posts (id, status) VALUES (1, 'draft')")
        with self.assertRaises(sqlite3.IntegrityError):
            cursor.execute("INSERT INTO posts (id, status) VALUES (2, 'deleted')")
        conn.close()


class TestAutogenerateEnumTypes(unittest.TestCase):
    """
    The native-type path of autogenerate, with the diff and the snapshot
    supplied directly: shared types create once, existing types are not
    recreated, and created types drop last on the way down.
    """

    def _generate(self, models, enum_types=None):
        diff = SchemaDiff()
        diff.missing_tables = list(models)
        snapshot = Snapshot(enum_types=enum_types or {})
        with (
            mock.patch("sustained.autogenerate.diff_schema", return_value=diff),
            mock.patch(
                "sustained.autogenerate.introspect_schema", return_value=snapshot
            ),
        ):
            return autogenerate(None, models, id="m", dialect=Dialects.POSTGRES)

    def setUp(self):
        class Article(Model):
            tableName = "articles"
            tableColumns = {
                "id": Integer(primary_key=True),
                "status": Enum("draft", "published", name="doc_status"),
            }
            _dialect = Dialects.POSTGRES

        class Page(Model):
            tableName = "pages"
            tableColumns = {
                "id": Integer(primary_key=True),
                "status": Enum("draft", "published", name="doc_status"),
            }
            _dialect = Dialects.POSTGRES

        self.Article = Article
        self.Page = Page

    def test_shared_type_created_once_and_dropped_last(self):
        migration = self._generate([self.Article, self.Page])
        creates = [s for s in migration.up if s.startswith("CREATE TYPE")]
        self.assertEqual(
            creates, ["CREATE TYPE \"doc_status\" AS ENUM ('draft', 'published')"]
        )
        self.assertEqual(migration.up[0], creates[0])
        self.assertEqual(migration.down[-1], 'DROP TYPE "doc_status"')
        self.assertEqual(
            [s for s in migration.down if s.startswith("DROP TABLE")],
            ['DROP TABLE IF EXISTS "pages"', 'DROP TABLE IF EXISTS "articles"'],
        )

    def test_existing_type_not_recreated(self):
        migration = self._generate(
            [self.Article], enum_types={"doc_status": ("draft", "published")}
        )
        self.assertFalse(any(s.startswith("CREATE TYPE") for s in migration.up))
        self.assertFalse(any(s.startswith("DROP TYPE") for s in migration.down))

    def test_conflicting_shared_type_rejected(self):
        class Other(Model):
            tableName = "others"
            tableColumns = {
                "id": Integer(primary_key=True),
                "status": Enum("open", "closed", name="doc_status"),
            }
            _dialect = Dialects.POSTGRES

        with self.assertRaisesRegex(ValueError, "different\\s+values in two models"):
            self._generate([self.Article, Other])


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestDuckDbEnumRoundTrip(unittest.TestCase):
    def test_create_insert_and_reject(self):
        class Doc(Model):
            tableName = "docs"
            tableColumns = {
                "id": Integer(primary_key=True),
                "state": Enum("new", "done", name="doc_state"),
            }
            _dialect = Dialects.DUCKDB

        conn = duckdb.connect(":memory:")
        cursor = conn.cursor()
        for statement in Doc.create_table_statements():
            cursor.execute(statement)
        cursor.execute("INSERT INTO docs VALUES (1, 'new')")
        with self.assertRaises(Exception):
            cursor.execute("INSERT INTO docs VALUES (2, 'gone')")
        for statement in Doc.drop_table_statements():
            cursor.execute(statement)
        conn.close()


class TestDropTypeIsDestructive(unittest.TestCase):
    def test_no_drops_blocks_drop_type(self):
        guard = no_drops()
        verdicts = guard(['DROP TYPE "post_status"'], Dialects.POSTGRES)
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0].rule, "no_drops")
        self.assertEqual(verdicts[0].verdict, "block")

    def test_destructive_labels_drop_type(self):
        labelled = destructive_statements(['DROP TYPE "post_status"'])
        self.assertEqual(labelled, ['DROP TYPE "post_status"'])

    def test_drop_constraint_still_passes(self):
        guard = no_drops()
        statement = "ALTER TABLE t DROP CONSTRAINT ck_t_status_enum"
        self.assertEqual(guard([statement], Dialects.POSTGRES), [])
        self.assertEqual(destructive_statements([statement]), [])


if __name__ == "__main__":
    unittest.main()
