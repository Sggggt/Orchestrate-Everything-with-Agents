"""Public workflow examples build and execute with the installed graph contract."""

from pathlib import Path
import re
import runpy
import tempfile
import unittest

from oea.runtime.host import Host

ROOT = Path(__file__).resolve().parents[1]


class PublicExamples(unittest.IsolatedAsyncioTestCase):
    async def test_readme_and_workflow_guide_use_the_shared_template_schema(self):
        for filename in ("README.md", "README.zh-CN.md", "docs/workflows.md"):
            source = (ROOT / filename).read_text(encoding="utf-8")
            blocks = re.findall(r"```python\n(.*?)```", source, re.S)
            block = next(value for value in blocks if "WorkflowBuilder" in value)
            namespace = {}
            exec(compile(block, filename, "exec"), namespace)
            graph = namespace["graph"]
            node = next(node for node in graph.nodes if node.type == "template")
            self.assertIn("text", node.config.value)

    async def test_executable_workflow_example_returns_its_documented_output(self):
        graph = runpy.run_path(str(ROOT / "examples/workflow.py"))["graph"]()
        with tempfile.TemporaryDirectory(prefix="oea-public-example-") as temporary:
            async with Host(Path(temporary) / "state") as host:
                await host.projects.create("Example", project_id="p")
                workflow = await host.workflows.create("p", "Example", graph)
                receipt = await host.workflows.run(workflow["id"], {"topic": "verified"}, "one", project_id="p")
                result = await host.workflows.result(receipt["id"], project_id="p", timeout=5)
                self.assertEqual(result["text"], "### 工作目标\n\nverified")
