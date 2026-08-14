"""
Tests for builder robustness: copy support, private attribute handling,
and validation of row-count arguments.
"""

import copy
import pickle
import unittest

from sustained import Model, create_model

User = create_model("RobustnessUser", "users")


class PickleUser(Model):
    tableName = "users"


class TestCopyAndPickle(unittest.TestCase):
    def test_deepcopy_does_not_recurse(self):
        query = User.query().select("id").where("id", "=", 1)
        clone = copy.deepcopy(query)
        self.assertEqual(str(clone), str(query))

    def test_deepcopy_is_independent(self):
        query = User.query().select("id")
        clone = copy.deepcopy(query)
        clone.where("id", "=", 1)
        self.assertNotIn("WHERE", str(query))
        self.assertIn("WHERE", str(clone))

    def test_copy_does_not_recurse(self):
        query = User.query().select("id")
        clone = copy.copy(query)
        self.assertEqual(str(clone), str(query))

    def test_model_instance_deepcopy(self):
        user = User(id=1, name="a")
        clone = copy.deepcopy(user)
        self.assertEqual(clone.id, 1)

    def test_model_instance_pickle_roundtrip(self):
        user = PickleUser(id=1)
        restored = pickle.loads(pickle.dumps(user))
        self.assertEqual(restored.id, 1)


class TestPrivateAttributeAccess(unittest.TestCase):
    def test_builder_private_attribute_raises(self):
        query = User.query()
        with self.assertRaises(AttributeError):
            query._not_a_real_attribute

    def test_model_private_attribute_raises(self):
        user = User()
        with self.assertRaises(AttributeError):
            user._not_a_real_column

    def test_model_dunder_attribute_raises(self):
        user = User()
        with self.assertRaises(AttributeError):
            user.__not_a_real_column__


class TestRowCountValidation(unittest.TestCase):
    def test_limit_rejects_bool(self):
        with self.assertRaises(TypeError):
            User.query().limit(True)

    def test_limit_rejects_negative(self):
        with self.assertRaises(ValueError):
            User.query().limit(-1)

    def test_offset_rejects_bool(self):
        with self.assertRaises(TypeError):
            User.query().offset(False)

    def test_offset_rejects_negative(self):
        with self.assertRaises(ValueError):
            User.query().offset(-5)

    def test_top_rejects_bool(self):
        with self.assertRaises(TypeError):
            User.query().top(True)

    def test_top_rejects_negative(self):
        with self.assertRaises(ValueError):
            User.query().top(-2)

    def test_limit_zero_allowed(self):
        self.assertIn("LIMIT 0", str(User.query().limit(0)))


if __name__ == "__main__":
    unittest.main()
