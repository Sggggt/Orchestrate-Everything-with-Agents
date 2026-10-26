"""Shared Agent configuration preserves fields and rejects retired contracts."""

import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from oea.contracts.configuration import AgentDefinition, Manager, Model


class ConfigurationTests(unittest.TestCase):
    def test_complete_configuration_round_trips_without_losing_groups(self):
        value = AgentDefinition(id="reviewer", name="Reviewer", manager=Manager(model=Model(model="coordinator")))
        encoded = value.model_dump_json()
        self.assertEqual(AgentDefinition.model_validate_json(encoded), value)
        self.assertEqual(value.context.input_tokens, 32768)
        self.assertEqual(value.model.max_output_tokens, 4096)
        self.assertEqual(value.manager.model.model, "coordinator")
        self.assertEqual(value.tools.local, ())

    def test_removed_capacity_and_flat_model_fields_are_rejected(self):
        for fields in ({"max_concurrency": 4}, {"reserved_external_slots": 1}, {"base_url": "https://example.invalid/v1"}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                AgentDefinition(id="reviewer", name="Reviewer", **fields)

    def test_environment_configuration_keeps_only_a_secret_reference(self):
        with patch.dict(os.environ, {"TEST_MODEL": "review", "TEST_API_KEY": "private-value", "TEST_BASE_URL": "https://example.invalid/v1"}):
            model = Model.from_env(prefix="TEST")
        self.assertEqual(model.credential_ref, "env:TEST_API_KEY")
        self.assertEqual(model.model, "review")
        self.assertNotIn("private-value", model.model_dump_json())

    def test_manager_schema_cannot_grant_business_tools(self):
        with self.assertRaises(ValidationError):
            Manager(tools={"local": ["run_command"]})

    def test_too_small_model_window_is_rejected(self):
        with self.assertRaises(ValidationError):
            Model(context_window_tokens=4096, max_output_tokens=4096)
