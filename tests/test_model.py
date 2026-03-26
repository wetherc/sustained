import unittest
from typing import Dict

from sustained.model import Model, create_model
from sustained.types import RelationMapping, RelationType


class TestModel(unittest.TestCase):
    def test_init_and_repr(self):
        class MyModel(Model):
            tableName = "my_table"

        instance = MyModel(id=1, name="Test")
        self.assertEqual(instance.id, 1)
        self.assertEqual(instance.name, "Test")
        self.assertIn("id=1", repr(instance))
        self.assertIn("name='Test'", repr(instance))
        self.assertIn("MyModel", repr(instance))

    def test_getattr_fully_qualified(self):
        class MyModel(Model):
            database = "mydb"
            tableSchema = "myschema"
            tableName = "my_table"

        model_instance = MyModel()
        self.assertEqual(model_instance.id, "mydb.myschema.my_table.id")
        self.assertEqual(model_instance.name, "mydb.myschema.my_table.name")

    def test_getattr_table_only(self):
        class MyModel(Model):
            tableName = "my_table"

        model_instance = MyModel()
        self.assertEqual(model_instance.id, "my_table.id")
        self.assertEqual(model_instance.name, "my_table.name")

    def test_getattr_no_table_name_raises_attribute_error(self):
        class MyModel(Model):
            pass

        model_instance = MyModel()
        with self.assertRaisesRegex(
            AttributeError, "'MyModel' object has no attribute 'id'"
        ):
            _ = model_instance.id

    def test_getattr_dunder_method_raises_attribute_error(self):
        class MyModel(Model):
            tableName = "my_table"

        model_instance = MyModel()
        with self.assertRaisesRegex(
            AttributeError, "'MyModel' object has no attribute '__deepcopy__'"
        ):
            _ = model_instance.__deepcopy__

    def test_create_model_basic(self):
        DynamicModel = create_model("DynamicModel", "dynamic_table")
        self.assertEqual(DynamicModel.__name__, "DynamicModel")
        self.assertEqual(DynamicModel.tableName, "dynamic_table")
        self.assertIsInstance(DynamicModel(), DynamicModel)
        self.assertEqual(DynamicModel.query().select("id")._model_class, DynamicModel)

    def test_create_model_with_mappings(self):
        mappings: Dict[str, RelationMapping] = {
            "test_relation": {
                "relation": RelationType.BelongsToOneRelation,
                "modelClass": "AnotherModel",  # String reference for simplicity
                "join": {"from": "from_col", "to": "to_col"},
            }
        }
        DynamicModel = create_model("DynamicModel", "dynamic_table", mappings=mappings)
        self.assertEqual(DynamicModel.relationMappings, mappings)

    def test_create_model_with_table_schema(self):
        DynamicModel = create_model(
            "DynamicModel", "dynamic_table", table_schema="temp_schema"
        )
        self.assertEqual(DynamicModel.tableSchema, "temp_schema")

    def test_create_model_with_database(self):
        DynamicModel = create_model("DynamicModel", "dynamic_table", database="temp_db")
        self.assertEqual(DynamicModel.database, "temp_db")

    def test_create_model_all_options(self):
        mappings: Dict[str, RelationMapping] = {
            "test_relation": {
                "relation": RelationType.HasManyRelation,
                "modelClass": "RelatedModel",
                "join": {"from": "p_id", "to": "r_p_id"},
            }
        }
        DynamicModel = create_model(
            "DynamicModel",
            "full_table",
            mappings=mappings,
            table_schema="full_schema",
            database="full_db",
        )
        self.assertEqual(DynamicModel.__name__, "DynamicModel")
        self.assertEqual(DynamicModel.tableName, "full_table")
        self.assertEqual(DynamicModel.relationMappings, mappings)
        self.assertEqual(DynamicModel.tableSchema, "full_schema")
        self.assertEqual(DynamicModel.database, "full_db")


if __name__ == "__main__":
    unittest.main()
