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


if __name__ == "__main__":
    unittest.main()
