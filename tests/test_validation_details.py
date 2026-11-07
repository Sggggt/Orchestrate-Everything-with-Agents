"""Bounded field diagnostics describe constraints without reflecting input."""

import json
import unittest

from oea.contracts.errors import RuntimeFault
from oea.contracts.schema_validation import schema_validator, validate_contract


class ValidationTests(unittest.TestCase):
    def errors(self, schema, value):
        with self.assertRaises(RuntimeFault) as caught:
            validate_contract(schema_validator(schema), value)
        return caught.exception.details["errors"]

    def test_nested_required_fields_and_extra_keys_are_safe(self):
        schema = {"type": "object", "properties": {"settings": {"type": "object", "required": ["name", "count"],
                  "properties": {"name": {"type": "string"}, "count": {"type": "integer", "minimum": 1}}, "additionalProperties": False}}}
        errors = self.errors(schema, {"settings": {"extra": "sensitive-input-value"}})
        self.assertEqual([item["path"] for item in errors[:2]], [["settings", "name"], ["settings", "count"]])
        self.assertIn("additionalProperties", [item["keyword"] for item in errors])
        self.assertNotIn("sensitive-input-value", json.dumps(errors))

    def test_combination_array_paths_and_many_violations_stay_bounded(self):
        schema = {"type": "array", "items": {"anyOf": [{"type": "integer", "minimum": 3}, {"type": "object", "required": ["name"]}]}}
        errors = self.errors(schema, ["sensitive-input-value"] * 10000)
        self.assertLessEqual(len(errors), 5)
        self.assertEqual(errors[0]["path"], [0])
        self.assertEqual(errors[0]["keyword"], "anyOf")
        self.assertTrue(all("schema_path" in error for error in errors))
        self.assertNotIn("sensitive-input-value", json.dumps(errors))
