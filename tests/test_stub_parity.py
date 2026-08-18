"""
The .pyi stubs are hand written, and most of the query surface is resolved
by __getattr__, so a type checker cannot catch a stub that has drifted from
the runtime. These tests compare the two directly.
"""

import ast
import unittest
from pathlib import Path

from sustained import Model
from sustained.builders import JoinClauseBuilder, OnClauseBuilder
from sustained.builders.conditional_clause_builder import ConditionalClauseBuilder
from sustained.builders.having_builder import HavingClauseBuilder
from sustained.builders.where_builder import WhereClauseBuilder

SRC = Path(__file__).resolve().parents[1] / "src" / "sustained"


class StubModel(Model):
    tableName = "stub_parity"


def base_names(node):
    """
    The names of the classes one stub class inherits from, in the same file.
    A base written as _Clauses["QueryBuilder[TModel]"] is a Subscript, so the
    subscript is peeled off first.
    """
    for base in node.bases:
        if isinstance(base, ast.Subscript):
            base = base.value
        if isinstance(base, ast.Name):
            yield base.id


def stub_methods(relative_path, class_name):
    """
    Every method name a stub class offers, its own and its bases'. Generic
    is skipped: it comes from typing, not from the stub.
    """
    tree = ast.parse((SRC / relative_path).read_text())
    classes = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }
    if class_name not in classes:
        raise AssertionError(f"{class_name} is not declared in {relative_path}")

    names, pending = set(), [class_name]
    while pending:
        node = classes.get(pending.pop())
        if node is None:
            continue
        names.update(
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not item.name.startswith("__")
        )
        pending.extend(base_names(node))
    return names


def runtime_definitions(relative_path):
    """
    The public surface a runtime module declares: top level functions and
    classes, plus the methods of every public class. Names that start with
    an underscore are private and are left out.
    """
    tree = ast.parse((SRC / relative_path).read_text())
    top, methods = set(), {}
    for node in tree.body:
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) or node.name.startswith("_"):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            top.add(node.name)
        else:
            top.add(node.name)
            methods[node.name] = {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not item.name.startswith("_")
            }
    return top, methods


def stub_definitions(relative_path):
    """Every public top level name a stub file declares."""
    tree = ast.parse((SRC / relative_path).read_text())
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    }


def resolves(obj, name):
    """
    Whether an attribute name reaches a callable. A clause method that
    rejects its position, such as andWhere as the first clause, still
    counts: it was found, then refused.
    """
    try:
        getattr(obj, name)
    except AttributeError:
        return False
    except RuntimeError:
        return True
    return True


class TestStubsMatchRuntime(unittest.TestCase):
    def assert_all_resolve(self, obj, names, source):
        missing = sorted(name for name in names if not resolves(obj, name))
        self.assertEqual(missing, [], f"{source} declares methods the runtime lacks")

    def test_query_builder_stub(self):
        self.assert_all_resolve(
            StubModel.query(),
            stub_methods("builder.pyi", "QueryBuilder"),
            "builder.pyi",
        )

    def test_write_builder_stub(self):
        """
        WriteBuilder is a stub-only view of the same runtime class, so its
        methods have to resolve on an ordinary query builder.
        """
        self.assert_all_resolve(
            StubModel.query().delete(),
            stub_methods("builder.pyi", "WriteBuilder"),
            "builder.pyi",
        )

    def test_join_builder_stub(self):
        self.assert_all_resolve(
            JoinClauseBuilder(StubModel),
            stub_methods("builders/join_builder.pyi", "JoinClauseBuilder"),
            "join_builder.pyi",
        )

    def test_on_clause_builder_stub(self):
        self.assert_all_resolve(
            OnClauseBuilder(),
            stub_methods("builders/join_builder.pyi", "OnClauseBuilder"),
            "join_builder.pyi",
        )

    def test_conditional_clause_builder_stub(self):
        names = stub_methods(
            "builders/conditional_clause_builder.pyi", "ConditionalClauseBuilder"
        )
        where_names = {n for n in names if "aving" not in n}
        having_names = names - where_names
        self.assert_all_resolve(
            WhereClauseBuilder(StubModel),
            where_names,
            "conditional_clause_builder.pyi",
        )
        self.assert_all_resolve(
            HavingClauseBuilder(StubModel),
            having_names,
            "conditional_clause_builder.pyi",
        )


class TestRuntimeSurfaceIsStubbed(unittest.TestCase):
    """The other direction: every dynamic method has a stub to be found by."""

    def test_every_join_type_is_stubbed(self):
        expected = set()
        for prefix in JoinClauseBuilder._JOIN_METHOD_MAP:
            base = f"{prefix}Join" if prefix else "join"
            expected.update({base, f"{base}Related"})

        for path, class_name in (
            ("builder.pyi", "QueryBuilder"),
            ("builders/join_builder.pyi", "JoinClauseBuilder"),
        ):
            self.assertEqual(
                sorted(expected - stub_methods(path, class_name)),
                [],
                f"{path} is missing join methods the runtime accepts",
            )

    def test_every_public_builder_name_is_stubbed(self):
        """
        builder.pyi hides builder.py from a type checker, so a public name
        that the stub leaves out is a name no checker can see. The runtime
        has one builder class; the stub splits it into a read view and a
        write view, so a method counts as stubbed when either view, or a
        base of either, declares it.
        """
        top, methods = runtime_definitions("builder.py")
        stubbed = stub_definitions("builder.pyi")
        self.assertEqual(
            sorted(top - stubbed),
            [],
            "builder.pyi is missing top level names builder.py declares",
        )

        views = stub_methods("builder.pyi", "QueryBuilder") | stub_methods(
            "builder.pyi", "WriteBuilder"
        )
        for class_name, names in methods.items():
            self.assertEqual(
                sorted(names - views),
                [],
                f"builder.pyi is missing {class_name} methods builder.py declares",
            )

    def test_every_clause_method_is_stubbed(self):
        expected = set()
        for where_name in ConditionalClauseBuilder._WHERE_METHOD_MAP:
            having_name = where_name.replace("where", "having", 1)
            for base in (where_name, having_name):
                capitalized = base[0].upper() + base[1:]
                expected.update({base, f"and{capitalized}", f"or{capitalized}"})

        for path, class_name in (
            ("builder.pyi", "QueryBuilder"),
            ("builders/conditional_clause_builder.pyi", "ConditionalClauseBuilder"),
        ):
            self.assertEqual(
                sorted(expected - stub_methods(path, class_name)),
                [],
                f"{path} is missing clause methods the runtime accepts",
            )


class TestDeclaredSurfaceResolves(unittest.TestCase):
    """
    ddl and schema are typed inline and carry no stub, so their __all__
    lists are the declared surface. A name listed there but not defined,
    or defined publicly but not listed, is the same drift a stale stub
    would be.
    """

    @staticmethod
    def declared_all(relative_path):
        tree = ast.parse((SRC / relative_path).read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                getattr(target, "id", None) == "__all__" for target in node.targets
            ):
                return {constant.value for constant in node.value.elts}
        raise AssertionError(f"{relative_path} declares no __all__")

    def test_every_ddl_name_is_exported_and_resolves(self):
        import sustained.ddl as ddl

        top, _ = runtime_definitions("ddl.py")
        exported = self.declared_all("ddl.py")
        self.assertEqual(sorted(top - exported), [], "ddl.py hides public names")
        for name in exported:
            self.assertTrue(hasattr(ddl, name), f"ddl.__all__ lists missing {name}")

    def test_every_schema_export_resolves(self):
        import sustained.schema as schema

        for name in self.declared_all("schema.py"):
            self.assertTrue(
                hasattr(schema, name), f"schema.__all__ lists missing {name}"
            )


if __name__ == "__main__":
    unittest.main()
