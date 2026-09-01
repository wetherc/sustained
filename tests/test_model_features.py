"""
Tests for class-level column access, declared columns, the model registry,
and relation mapping validation.
"""

import sys
import unittest

from sustained import Model, RelationType, create_model
from sustained.model import (
    _register_model,
    get_registered_model,
    resolve_model_reference,
)


class ColUser(Model):
    tableName = "users"
    columns = ("id", "name", "email")


class TestClassLevelColumnAccess(unittest.TestCase):
    def test_class_attribute_returns_qualified_column(self):
        self.assertEqual(ColUser.id, "users.id")

    def test_instance_attribute_needs_the_column_to_be_loaded(self):
        self.assertEqual(ColUser(name="Ada").name, "Ada")
        with self.assertRaises(AttributeError):
            ColUser().name

    def test_schema_and_database_included(self):
        Qualified = create_model(
            "QualifiedModel", "tbl", table_schema="dbo", database="db"
        )
        self.assertEqual(Qualified.id, "db.dbo.tbl.id")

    def test_class_without_table_name_raises(self):
        with self.assertRaises(AttributeError):
            Model.some_column


class TestDeclaredColumns(unittest.TestCase):
    def test_undeclared_column_raises_on_class(self):
        with self.assertRaises(AttributeError):
            ColUser.nickname

    def test_undeclared_column_raises_on_instance(self):
        with self.assertRaises(AttributeError):
            ColUser().nickname

    def test_declared_column_allowed(self):
        self.assertEqual(ColUser.email, "users.email")

    def test_undeclared_models_stay_permissive(self):
        Loose = create_model("LooseModel", "loose")
        self.assertEqual(Loose.anything, "loose.anything")

    def test_create_model_accepts_columns(self):
        Strict = create_model("StrictModel", "strict_tbl", columns=("a",))
        self.assertEqual(Strict.a, "strict_tbl.a")
        with self.assertRaises(AttributeError):
            Strict.b


class TestModelRegistry(unittest.TestCase):
    def test_string_relation_resolves_through_registry(self):
        Pet = create_model("RegistryPet", "pets")
        Owner = create_model(
            "RegistryOwner",
            "owners",
            mappings={
                "pets": {
                    "relation": RelationType.HasManyRelation,
                    "modelClass": "RegistryPet",
                    "join": {"from": "owners.id", "to": "pets.ownerId"},
                }
            },
        )
        sql = str(Owner.query().joinRelated("pets"))
        self.assertIn("JOIN pets ON owners.id = pets.ownerId", sql)

    def test_unresolvable_string_reference_raises(self):
        Owner = create_model(
            "RegistryOrphan",
            "owners",
            mappings={
                "pets": {
                    "relation": RelationType.HasManyRelation,
                    "modelClass": "NoSuchModelAnywhere",
                    "join": {"from": "owners.id", "to": "pets.ownerId"},
                }
            },
        )
        with self.assertRaises(ValueError):
            Owner.query().joinRelated("pets")


def _make_twin(table_name):
    """Defines a class named TwinModel, so two calls collide by name."""

    class TwinModel(Model):
        tableName = table_name

    return TwinModel


def _make_other_twin(table_name):
    """Defines a second TwinModel in another place in this module."""

    class TwinModel(Model):
        tableName = table_name

    return TwinModel


class TestAmbiguousModelNames(unittest.TestCase):
    def setUp(self):
        self.first = _make_twin("first_twins")
        self.second = _make_other_twin("second_twins")

    def test_registry_lookup_raises(self):
        with self.assertRaises(ValueError) as caught:
            get_registered_model("TwinModel")
        message = str(caught.exception)
        self.assertIn("ambiguous", message)
        self.assertIn(f"{__name__}._make_twin.<locals>.TwinModel", message)
        self.assertIn(f"{__name__}._make_other_twin.<locals>.TwinModel", message)

    def test_the_same_place_is_listed_once(self):
        _make_twin("more_twins")
        with self.assertRaises(ValueError) as caught:
            get_registered_model("TwinModel")
        message = str(caught.exception)
        self.assertEqual(message.count("_make_twin.<locals>"), 1)

    def test_string_reference_raises_without_a_context_module(self):
        with self.assertRaises(ValueError):
            resolve_model_reference("TwinModel")

    def test_neither_class_wins(self):
        # The first registration is not kept and the second does not
        # replace it, so nothing resolves to the wrong class.
        with self.assertRaises(ValueError):
            resolve_model_reference("TwinModel", context_module=__name__)

    def test_context_module_decides(self):
        setattr(sys.modules[__name__], "TwinModel", self.second)
        self.addCleanup(delattr, sys.modules[__name__], "TwinModel")
        self.assertIs(
            resolve_model_reference("TwinModel", context_module=__name__),
            self.second,
        )

    def test_a_third_place_joins_the_list(self):
        def make_third():
            class TwinModel(Model):
                tableName = "third_twins"

            return TwinModel

        third = make_third()
        with self.assertRaises(ValueError) as caught:
            get_registered_model("TwinModel")
        self.assertIn(third.__qualname__, str(caught.exception))

    def test_registering_a_shared_class_again_changes_nothing(self):
        _register_model("TwinModel", self.first)
        with self.assertRaises(ValueError):
            get_registered_model("TwinModel")

    def test_class_object_is_not_a_collision(self):
        Solo = create_model("SoloTwin", "solos")
        _register_model("SoloTwin", Solo)
        self.assertIs(get_registered_model("SoloTwin"), Solo)


class TestRelationValidation(unittest.TestCase):
    def test_missing_join_keys_raise(self):
        Pet = create_model("ValidationPet", "pets")
        Owner = create_model(
            "ValidationOwner",
            "owners",
            mappings={
                "pets": {
                    "relation": RelationType.HasManyRelation,
                    "modelClass": Pet,
                    "join": {"from": "owners.id"},
                }
            },
        )
        with self.assertRaises(ValueError):
            Owner.query().joinRelated("pets")

    def test_alias_with_unqualified_to_column_does_not_crash(self):
        Pet = create_model("AliasPet", "pets")
        Owner = create_model(
            "AliasOwner",
            "owners",
            mappings={
                "pets": {
                    "relation": RelationType.HasManyRelation,
                    "modelClass": Pet,
                    "join": {"from": "owners.id", "to": "ownerId"},
                }
            },
        )
        sql = str(Owner.query().joinRelated("pets", alias="p"))
        self.assertIn("JOIN pets AS p", sql)


if __name__ == "__main__":
    unittest.main()
