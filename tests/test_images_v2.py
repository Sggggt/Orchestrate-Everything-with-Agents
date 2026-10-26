"""Image evidence stays referenced until the actual model request boundary."""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from oea import AgentDefinition, Completion, ContextPolicy, Model, RunSpec, Tools, Workspace
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import rows


class ImageModel:
    """Protocol fixture observes the real outgoing model message representation."""
    def __init__(self, path):
        self.path, self.inputs = path, []

    async def complete(self, model, messages, **options):
        self.inputs.append(messages)
        if len(self.inputs) == 1:
            return Completion(tool_calls=({"id": "image-evidence", "type": "function", "function": {"name": "read_image", "arguments": json.dumps({"path": str(self.path)})}},),
                finish_reason="tool_calls", usage={"prompt_tokens": 60, "completion_tokens": 6})
        return Completion(text="Image evidence received", usage={"prompt_tokens": 2200, "completion_tokens": 6})


class BatchImageModel(ImageModel):
    def __init__(self, paths):
        self.paths, self.inputs = paths, []

    async def complete(self, model, messages, **options):
        self.inputs.append(messages)
        if len(self.inputs) == 1:
            return Completion(tool_calls=tuple({"id": "image-" + str(index), "type": "function",
                "function": {"name": "read_image", "arguments": json.dumps({"path": str(path)})}}
                for index, path in enumerate(self.paths)), finish_reason="tool_calls",
                usage={"prompt_tokens": 60, "completion_tokens": 6})
        return Completion(text="Image batch received", usage={"prompt_tokens": 9000, "completion_tokens": 6})


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-image-")
        self.root = Path(self.temporary.name)
        self.project = self.root / "图片 资料"
        self.project.mkdir()
        # A PNG carrying a JPEG suffix verifies that transport MIME follows
        # decoded image content rather than the caller's filename.
        self.image = self.project / "证据.jpg"
        Image.new("RGB", (32, 24), (17, 37, 53)).save(self.image, format="PNG")
        self.model = ImageModel(self.image)
        self.host = await Host(self.root / "state", model_api=self.model).__aenter__()
        await self.host.projects.create("Project", project_id="p", folder=self.project)
        await self.host.agents.save(AgentDefinition(id="review", name="Review", project_id="p", model=Model(base_url="https://example.invalid/v1", model="image-fixture"),
            tools=Tools(local=("read_image",)), workspace=Workspace(project=self.project), context=ContextPolicy(input_tokens=131072)))
        session = await self.host.sessions.create("p")
        self.instance = await self.host.instances.create(session["id"], "review")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def submit(self):
        return await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Inspect the image evidence", idempotency_key="image"))

    async def test_image_encoded_only_on_outgoing_boundary_with_original_mime(self):
        original = self.image.read_bytes()
        run = await self.submit()
        result = await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(result.text, "Image evidence received")
        parts = [part for message in self.model.inputs[-1] if isinstance(message.get("content"), list) for part in message["content"] if part["type"] == "image_url"]
        self.assertEqual(len(parts), 1)
        url = parts[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), original)
        conversation = await self.host.contexts.history(self.instance["id"])
        stored = json.dumps(conversation)
        self.assertIn("oea_image", stored)
        self.assertNotIn("base64,", stored)
        graph = await self.host.graph.aget_state({"configurable": {"thread_id": run.id}})
        self.assertNotIn("base64,", json.dumps(graph.values))
        self.assertEqual(self.image.read_bytes(), original)
        self.assertEqual(result.artifacts[0].media_type, "image/png")

    async def test_revocation_before_request_does_not_send_old_image(self):
        from oea.runtime.context import RunContext
        original = RunContext.model_messages
        revoked = False
        async def before_request(context, messages):
            nonlocal revoked
            if any(isinstance(message.get("content"), list) for message in messages) and not revoked:
                revoked = True
                definition = (await self.host.agents.get("review"))["definition"]
                definition["tools"]["local"] = []
                await self.host.agents.save(definition)
            return await original(context, messages)
        with patch.object(RunContext, "model_messages", before_request):
            run = await self.submit()
            with self.assertRaises(RuntimeFault) as error:
                await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(error.exception.code, "PERMISSION_DENIED")
        self.assertEqual(len(self.model.inputs), 1)
        receipts = await self.host.store.call(lambda db: rows(db, "SELECT state FROM actions WHERE run_id=? AND name='read_image'", (run.id,)))
        self.assertEqual(receipts, [{"state": "completed"}])

    async def test_legal_multiple_images_can_exceed_old_eight_mib_envelope(self):
        # Valid PNGs with inert trailing bytes exercise transport size without
        # allocating large pixel arrays or committing image data as fixtures.
        data = self.image.read_bytes() + b"\0" * (3 * 1024 * 1024)
        paths = [self.project / ("batch-" + str(index) + ".png") for index in range(2)]
        for path in paths:
            path.write_bytes(data)
        self.model = BatchImageModel(paths)
        self.host.model_api = self.model
        run = await self.submit()
        result = await self.host.runs.result(run.id, timeout=20)
        self.assertEqual(result.text, "Image batch received")
        parts = [part for message in self.model.inputs[-1] if isinstance(message.get("content"), list)
                 for part in message["content"] if part["type"] == "image_url"]
        self.assertEqual(len(parts), 2)
        self.assertGreater(sum(len(part["image_url"]["url"]) for part in parts), 8 * 1024 * 1024)
        for part in parts:
            self.assertEqual(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]), data)
        self.assertNotIn("base64,", json.dumps(await self.host.contexts.history(self.instance["id"])))

    async def test_image_batch_above_shared_bound_is_rejected_before_provider(self):
        data = self.image.read_bytes() + b"\0" * (4 * 1024 * 1024)
        paths = [self.project / ("large-" + str(index) + ".png") for index in range(8)]
        for path in paths:
            path.write_bytes(data)
        self.model = BatchImageModel(paths)
        self.host.model_api = self.model
        run = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(run.id, timeout=20)
        self.assertEqual(caught.exception.code, "MODEL_REQUEST_TOO_LARGE")
        self.assertFalse(caught.exception.details["request_sent"])
        self.assertEqual(len(self.model.inputs), 1)
