"""The canonical SDK must use the same types and state as existing integrations."""

import tempfile
import unittest
from unittest.mock import patch

import ma_runtime
import oea
from oea.stdio import run_agent
from agent.stdio_sdk import run_agent as legacy_run_agent


class PublicSDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_importing_either_brand_of_the_platform_bridge_is_rejected(self):
        entries = {
            "canonical-module": {"command": "python", "args": ["-m", "oea", "mcp"]},
            "canonical-command": {"command": "oea.exe", "args": ["mcp"]},
            "legacy-module": {"command": "python", "args": ["-m", "ma_runtime.cli", "mcp"]},
            "legacy-command": {"command": "multi-agent", "args": ["mcp"]},
        }
        with tempfile.TemporaryDirectory(prefix="oea-import-") as directory:
            async with oea.Runtime.open(directory) as runtime:
                with patch.object(runtime.mcp, "_codex_entries", return_value=entries):
                    result = await runtime.mcp.import_codex(list(entries))
                self.assertEqual(result["imported"], [])
                self.assertEqual(len(result["errors"]), len(entries))
                self.assertEqual({item["error"]["code"] for item in result["errors"]}, {"MCP_IMPORT_UNSUPPORTED"})

    async def test_canonical_namespace_shares_types_and_generates_connectable_bridge(self):
        self.assertIs(oea.Runtime, ma_runtime.Runtime)
        self.assertIs(oea.TaskSpec, ma_runtime.TaskSpec)
        self.assertIs(run_agent, legacy_run_agent)
        with tempfile.TemporaryDirectory(prefix="oea-sdk-") as directory:
            async with oea.Runtime.open(directory) as runtime:
                config = (await runtime.mcp.codex_config())["mcp_servers"]["oea"]
                self.assertEqual(config["args"][1:3], ["-m", "oea"])
                self.assertIn("--connect", config["args"])
                self.assertEqual(config["enabled_tools"], ["platform_guide", "platform_call"])


if __name__ == "__main__":
    unittest.main()
