"""Function declarations share input, invocation, and JSON result semantics."""

import unittest
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import jsonschema
from pydantic import BaseModel

from oea.contracts.errors import RuntimeFault
from oea.tools.functions import tool


@dataclass
class Receipt:
    count: int
    path: Path


class Record(BaseModel):
    name: str
    count: int


class Status(Enum):
    READY = "ready"


def record_item(name: str, count: int = 1) -> Record:
    return Record(name=name, count=count)


class FunctionToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_dataclass_result_is_json_before_schema_validation(self):
        calls = []

        def submit(count: int) -> Receipt:
            calls.append(count)
            return Receipt(count=count, path=Path("output.txt"))

        declared = tool(submit, side_effects="local")
        result = await declared.invoke({"arguments": {"count": 3}})
        jsonschema.validate(result, declared.describe().output_schema)
        self.assertEqual(result, {"count": 3, "path": "output.txt"})
        self.assertEqual(calls, [3])

    async def test_invalid_input_is_rejected_before_the_action(self):
        calls = []

        def submit(count: int) -> int:
            calls.append(count)
            return count

        declared = tool(submit)
        with self.assertRaises(RuntimeFault) as caught:
            await declared.invoke({"arguments": {"count": "invalid"}})
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertEqual(calls, [])

    async def test_model_defaults_and_nested_typed_arguments_survive(self):
        declared = tool(record_item)
        self.assertEqual(await declared.invoke({"arguments": {"name": "record"}}), {"name": "record", "count": 1})

        @tool
        async def count_records(record: Record) -> list[int]:
            return [record.count]

        result = await count_records.invoke({"arguments": {"record": {"name": "r", "count": 4}}})
        self.assertEqual(result, {"value": [4]})
        jsonschema.validate(result, count_records.describe().output_schema)

    def test_variadic_or_untyped_parameters_are_rejected_at_declaration(self):
        with self.assertRaises(TypeError):
            tool(lambda value: value)

        def variadic(*items: int) -> int:
            return len(items)

        with self.assertRaises(TypeError):
            tool(variadic)

    def test_threaded_functions_cannot_declare_unimplemented_cancellation(self):
        self.assertEqual(tool(record_item).describe().cancellation, "none")
        with self.assertRaises(ValueError):
            tool(record_item, cancellation="cooperative")

        async def cooperative() -> int:
            return 1

        self.assertEqual(tool(cooperative, cancellation="cooperative").describe().cancellation, "cooperative")
        with self.assertRaises(ValueError):
            tool(cooperative, cancellation="owned_process")

    async def test_referenced_scalar_and_collection_result_schemas_resolve(self):
        @tool
        def status() -> Status:
            return Status.READY

        result = await status.invoke({"arguments": {}})
        self.assertEqual(result, {"value": "ready"})
        jsonschema.validate(result, status.describe().output_schema)

        @tool
        def statuses() -> list[Status]:
            return [Status.READY]

        result = await statuses.invoke({"arguments": {}})
        self.assertEqual(result, {"value": ["ready"]})
        jsonschema.validate(result, statuses.describe().output_schema)
