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
from sustained.autogenerate import SchemaDiff, autogenerate, diff_schema
from sustained.dialects import Dialects
from sustained.guards import no_drops
from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedTable,
    Snapshot,
    introspect_schema,
)
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
    The native-type path of autogenerate, with the snapshot supplied
    directly: shared types create once, existing types are not
    recreated, and created types drop last on the way down.
    """

    def _generate(self, models, enum_types=None, tables=None, enum_types_read=True):
        snapshot = Snapshot(
            tables=tables or {},
            enum_types=enum_types or {},
            enum_types_read=enum_types_read,
        )
        with mock.patch(
            "sustained.autogenerate.introspect_schema", return_value=snapshot
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

    def test_drop_constraint_is_destructive(self):
        guard = no_drops()
        statement = "ALTER TABLE t DROP CONSTRAINT ck_t_status_enum"
        verdicts = guard([statement], Dialects.POSTGRES)
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(destructive_statements([statement]), [statement])


def _posts_snapshot(enum_values=("draft", "published"), status_type="post_status"):
    """A snapshot of the posts table with its enum column and type."""
    status = IntrospectedColumn(
        raw_type=status_type,
        nullable=False,
        primary_key=False,
        default="'draft'::post_status",
        enum_name=status_type if status_type == "post_status" else None,
        enum_values=enum_values if status_type == "post_status" else (),
    )
    table = IntrospectedTable(
        columns={
            "id": IntrospectedColumn(
                raw_type="integer", nullable=False, primary_key=True
            ),
            "status": status,
        },
        primary_key=("id",),
    )
    enum_types = {"post_status": tuple(enum_values)}
    if status_type != "post_status":
        enum_types = {}
    return Snapshot(
        tables={"posts": table}, enum_types=enum_types, enum_types_read=True
    )


class TestEnumTypeDiffing(unittest.TestCase):
    """
    Value changes on a native enum type: an appended value generates
    ADD VALUE and loses the down; a removed or reordered value refuses
    with the rebuild recipe; a matching column diffs clean.
    """

    def _diff(self, model, snapshot):
        with mock.patch(
            "sustained.autogenerate.introspect_schema", return_value=snapshot
        ):
            return diff_schema(None, [model], dialect=Dialects.POSTGRES)

    def _generate(self, model, snapshot, **kwargs):
        with mock.patch(
            "sustained.autogenerate.introspect_schema", return_value=snapshot
        ):
            return autogenerate(
                None, [model], id="m", dialect=Dialects.POSTGRES, **kwargs
            )

    def test_matching_enum_column_diffs_clean(self):
        diff = self._diff(EnumPost, _posts_snapshot())
        self.assertTrue(diff.is_empty(), diff.summary())

    def test_appended_value_generates_add_value(self):
        class Post(Model):
            tableName = "posts"
            tableColumns = {
                "id": Integer(primary_key=True),
                "status": Enum(
                    "draft",
                    "published",
                    "archived",
                    name="post_status",
                    nullable=False,
                    default="draft",
                ),
            }

        diff = self._diff(Post, _posts_snapshot())
        self.assertEqual(
            diff.changed_enum_types,
            [
                (
                    "post_status",
                    ("draft", "published"),
                    ("draft", "published", "archived"),
                )
            ],
        )
        self.assertIn("add value 'archived' to enum type post_status", diff.summary())
        migration = self._generate(Post, _posts_snapshot())
        self.assertEqual(
            migration.up, ["ALTER TYPE \"post_status\" ADD VALUE 'archived'"]
        )
        self.assertIsNone(migration.down)

    def test_removed_value_refuses_with_recipe(self):
        class Post(Model):
            tableName = "posts"
            tableColumns = {
                "id": Integer(primary_key=True),
                "status": Enum(
                    "draft", name="post_status", nullable=False, default="draft"
                ),
            }

        diff = self._diff(Post, _posts_snapshot())
        self.assertEqual(len(diff.changed_enum_types), 1)
        self.assertIn(
            "enum type 'post_status' has values", "\n".join(diff.outstanding())
        )
        with self.assertRaisesRegex(ValueError, "creates a new type"):
            self._generate(Post, _posts_snapshot())

    def test_reordered_values_refuse(self):
        class Post(Model):
            tableName = "posts"
            tableColumns = {
                "id": Integer(primary_key=True),
                "status": Enum(
                    "published",
                    "draft",
                    name="post_status",
                    nullable=False,
                    default="draft",
                ),
            }

        with self.assertRaisesRegex(ValueError, "removed or reordered"):
            self._generate(Post, _posts_snapshot())

    def test_varchar_column_converts_with_a_type_cast(self):
        snapshot = _posts_snapshot(status_type="character varying(255)")
        diff = self._diff(EnumPost, snapshot)
        self.assertEqual(len(diff.changed_columns), 1)
        self.assertEqual(diff.new_enum_types, [("post_status", ("draft", "published"))])
        migration = self._generate(
            EnumPost,
            _posts_snapshot(status_type="character varying(255)"),
            type_casts={"posts.status": "status::post_status"},
        )
        self.assertEqual(
            migration.up[0],
            "CREATE TYPE \"post_status\" AS ENUM ('draft', 'published')",
        )
        self.assertIn(
            'ALTER TABLE posts ALTER COLUMN "status" TYPE "post_status" '
            "USING status::post_status",
            migration.up,
        )
        self.assertEqual(migration.down[-1], 'DROP TYPE "post_status"')


class TestEnumColumnAdded(unittest.TestCase):
    """A new enum column on an existing table, per strategy."""

    def _existing_table(self):
        return IntrospectedTable(
            columns={
                "id": IntrospectedColumn(
                    raw_type="integer", nullable=False, primary_key=True
                )
            },
            primary_key=("id",),
        )

    def _generate(self, dialect, snapshot):
        class Post(Model):
            tableName = "posts"
            tableColumns = {
                "id": Integer(primary_key=True),
                "status": Enum("draft", "published", name="post_status"),
            }
            _dialect = dialect

        with mock.patch(
            "sustained.autogenerate.introspect_schema", return_value=snapshot
        ):
            return autogenerate(None, [Post], id="m", dialect=dialect)

    def test_postgres_creates_the_type_before_the_column(self):
        snapshot = Snapshot(
            tables={"posts": self._existing_table()}, enum_types_read=True
        )
        migration = self._generate(Dialects.POSTGRES, snapshot)
        self.assertEqual(
            migration.up,
            [
                "CREATE TYPE \"post_status\" AS ENUM ('draft', 'published')",
                'ALTER TABLE "posts" ADD COLUMN "status" "post_status"',
            ],
        )
        self.assertEqual(
            migration.down,
            [
                'ALTER TABLE "posts" DROP COLUMN "status"',
                'DROP TYPE "post_status"',
            ],
        )

    def test_mssql_adds_the_check_beside_the_column(self):
        snapshot = Snapshot(tables={"posts": self._existing_table()})
        migration = self._generate(Dialects.MSSQL, snapshot)
        self.assertEqual(
            migration.up,
            [
                "ALTER TABLE [posts] ADD [status] NVARCHAR(9)",
                "ALTER TABLE [posts] ADD CONSTRAINT [ck_posts_status_enum] "
                "CHECK ([status] IN ('draft', 'published'))",
            ],
        )
        self.assertEqual(
            migration.down,
            [
                "ALTER TABLE [posts] DROP CONSTRAINT [ck_posts_status_enum]",
                "ALTER TABLE [posts] DROP COLUMN [status]",
            ],
        )

    def test_sqlite_rebuilds_the_table(self):
        class Post(Model):
            tableName = "posts"
            tableColumns = {
                "id": Integer(primary_key=True),
                "status": Enum(
                    "draft", "published", name="post_status", default="draft"
                ),
            }

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO posts (id) VALUES (1)")
        migration = autogenerate(conn, [Post], id="m")
        self.assertIsNone(migration.down)
        for statement in migration.up:
            conn.execute(statement)
        self.assertEqual(
            conn.execute("SELECT status FROM posts").fetchall(), [("draft",)]
        )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO posts (id, status) VALUES (2, 'gone')")
        conn.close()

    def test_not_null_enum_with_backfill_tightens_after_the_add(self):
        class Post(Model):
            tableName = "posts"
            tableColumns = {
                "id": Integer(primary_key=True),
                "status": Enum(
                    "draft",
                    "published",
                    name="post_status",
                    nullable=False,
                    backfill="draft",
                ),
            }
            _dialect = Dialects.POSTGRES

        snapshot = Snapshot(
            tables={"posts": self._existing_table()}, enum_types_read=True
        )
        with mock.patch(
            "sustained.autogenerate.introspect_schema", return_value=snapshot
        ):
            migration = autogenerate(None, [Post], id="m", dialect=Dialects.POSTGRES)
        self.assertEqual(
            migration.up,
            [
                "CREATE TYPE \"post_status\" AS ENUM ('draft', 'published')",
                'ALTER TABLE "posts" ADD COLUMN "status" "post_status"',
                'UPDATE "posts" SET "status" = \'draft\' WHERE "status" IS NULL',
                'ALTER TABLE "posts" ALTER COLUMN "status" SET NOT NULL',
            ],
        )


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestDuckDbEnumDiffing(unittest.TestCase):
    """
    DuckDB reports its enum types in duckdb_types(), so a type is known
    to be there whether or not a column still uses it. A value change is
    refused, since DuckDB cannot add a value in place.
    """

    def _model(self, *values):
        class Doc(Model):
            tableName = "docs"
            tableColumns = {
                "id": Integer(primary_key=True),
                "state": Enum(*values, name="doc_state"),
            }
            _dialect = Dialects.DUCKDB

        return Doc

    def test_existing_enum_column_diffs_clean(self):
        Doc = self._model("new", "done")
        conn = duckdb.connect(":memory:")
        cursor = conn.cursor()
        for statement in Doc.create_table_statements():
            cursor.execute(statement)
        diff = diff_schema(conn, [Doc], dialect=Dialects.DUCKDB)
        self.assertEqual(diff.new_enum_types, [])
        self.assertEqual(diff.changed_columns, [])
        conn.close()

    def test_value_change_is_refused(self):
        Doc = self._model("new", "done")
        conn = duckdb.connect(":memory:")
        cursor = conn.cursor()
        for statement in Doc.create_table_statements():
            cursor.execute(statement)
        Wider = self._model("new", "done", "archived")
        with self.assertRaises(DialectError):
            autogenerate(conn, [Wider], id="m", dialect=Dialects.DUCKDB)
        conn.close()

    def test_the_type_catalog_is_read(self):
        conn = duckdb.connect(":memory:")
        conn.cursor().execute("CREATE TYPE doc_state AS ENUM ('new', 'done')")
        schema = introspect_schema(conn, Dialects.DUCKDB)
        self.assertTrue(schema.enum_types_read)
        self.assertEqual(schema.enum_types["doc_state"], ("new", "done"))
        conn.close()

    def test_a_type_with_no_column_is_not_created_again(self):
        # The type is there but nothing uses it yet. Inferring presence
        # from the columns alone would generate a second CREATE TYPE,
        # which DuckDB refuses.
        conn = duckdb.connect(":memory:")
        conn.cursor().execute("CREATE TYPE doc_state AS ENUM ('new', 'done')")
        Doc = self._model("new", "done")
        diff = diff_schema(conn, [Doc], dialect=Dialects.DUCKDB)
        self.assertEqual(diff.new_enum_types, [])
        self.assertEqual([m.tableName for m in diff.missing_tables], ["docs"])
        migration = autogenerate(conn, [Doc], id="m", dialect=Dialects.DUCKDB)
        self.assertTrue(all("CREATE TYPE" not in step for step in migration.up))
        for statement in migration.up:
            conn.cursor().execute(statement)
        conn.close()


if __name__ == "__main__":
    unittest.main()
