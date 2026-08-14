"""
Tests for class-level column access, declared columns, the model registry,
and relation mapping validation.
"""

import unittest

from sustained import Model, RelationType, create_model


class ColUser(Model):
    tableName = "users"
    columns = ("id", "name", "email")


class TestClassLevelColumnAccess(unittest.TestCase):
    def test_class_attribute_returns_qualified_column(self):
        self.assertEqual(ColUser.id, "users.id")

    def test_instance_attribute_still_works(self):
        self.assertEqual(ColUser().name, "users.name")

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
