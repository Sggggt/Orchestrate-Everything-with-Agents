"""A separate application process uses real HTTP, model streaming and native tools."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import Request
from fastapi.responses import StreamingResponse
import uvicorn

from oea import AgentDefinition, ApplicationBinding, ApplicationGrant, Model, Tools
from oea.runtime.host import Host
from oea.web.app import create_app


@unittest.skipUnless(os.environ.get("OEA_TEST_NATIVE_COMPONENT"), "Native component is not configured")
class OAPNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_backend_stream_reconnect_upload_native_artifact_and_receipts(self):
        with tempfile.TemporaryDirectory(prefix="oap-net-") as temporary:
            root = Path(temporary)
            native = patch("oea.workspace.native.executable", return_value=Path(os.environ["OEA_TEST_NATIVE_COMPONENT"]))
            with native:
                async with Host(root / "state", allow_commands=True) as host:
                    listener = socket.socket()
                    listener.bind(("127.0.0.1", 0))
                    origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
                    app = create_app(host=host)
                    requests = []
                    final = "Network delivery — verified " + "完整结果 " * 40

                    @app.post("/v1/chat/completions")
                    async def completion(request: Request):
                        body = await request.json()
                        requests.append(body)
                        observed = [item for item in body["messages"] if item["role"] == "tool"]
                        if not observed:
                            arguments = {"argv": [sys.executable, "-B", "-c", "from pathlib import Path; Path('result.txt').write_text('verified artifact', encoding='utf-8'); print('written')"], "yield_time_ms": 1000}
                            call = {"index": 0, "id": "write", "type": "function", "function": {"name": "exec_command", "arguments": json.dumps(arguments)}}
                        elif len(observed) == 1:
                            call = {"index": 0, "id": "publish", "type": "function", "function": {"name": "publish_artifact", "arguments": json.dumps({"path": "result.txt"})}}
                        else:
                            call = None
                        async def packets():
                            if call:
                                yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}]}) + "\n\n"
                            else:
                                for offset in range(0, len(final), 20):
                                    yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": final[offset:offset + 20]}, "finish_reason": None}]}) + "\n\n"
                                    await asyncio.sleep(.01)
                            yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if call else "stop"}], "usage": {"prompt_tokens": 200, "completion_tokens": 60}}) + "\n\n"
                            yield "data: [DONE]\n\n"
                        return StreamingResponse(packets(), media_type="text/event-stream")

                    await host.projects.create("Example", project_id="p")
                    tools = Tools(local=("exec_command", "write_stdin", "publish_artifact"))
                    await host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url=origin + "/v1", model="protocol-fixture"), tools=tools))
                    registered = await host.applications.create("Application")
                    binding = await host.applications.bind(ApplicationBinding(app_id=registered["application"]["id"], project_id="p", agent_id="worker", grant=ApplicationGrant(tools=tools)))
                    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, proxy_headers=False, timeout_graceful_shutdown=2))
                    serving = asyncio.create_task(server.serve(sockets=[listener]))
                    process = None
                    try:
                        async with asyncio.timeout(10):
                            while not server.started:
                                if serving.done():
                                    await serving
                                await asyncio.sleep(.01)
                        program = """import asyncio,hashlib,json,os
from contextlib import aclosing
from pathlib import Path
from oea import ApplicationClient
async def main():
    async with ApplicationClient(os.environ['OAP_URL'],os.environ['OAP_CREDENTIAL']) as client:
        conversation=await client.create_conversation(os.environ['OAP_BINDING'],idempotency_key='conversation')
        data=b'assigned source'
        upload=await client.begin_upload(conversation['id'],'source.txt',len(data),hashlib.sha256(data).hexdigest(),idempotency_key='upload')
        async def chunks(): yield data
        await client.upload_content(upload['id'],chunks())
        run=await client.submit(conversation['id'],'Create and publish the result',idempotency_key='work',artifact_ids=(upload['id'],))
        position=0; delta=False
        async with aclosing(client.events(run['id'])) as events:
            async for event in events:
                if event['kind']=='event': position=event['position']
                if event['kind']=='model.delta':
                    assert 'sequence' in event and 'attempt_id' in event
                    delta=True; break
                assert event['kind'] not in ('error','run.failed','run.needs_review'),event
        async for event in client.events(run['id'],after=position):
            assert event['kind'] not in ('error','run.failed','run.needs_review'),event
        result=await client.result(run['id'])
        response=await client.response(run['id'])
        assert response['text']==result['text']
        actions=await client.actions(run['id'])
        original=next(item for item in actions['items'] if item['name']=='exec_command')
        receipt=await client.action(run['id'],original['id'])
        assert 'environment' not in receipt['result'] and 'cwd' not in receipt['result']
        output=await client.download_command(run['id'],original['id'],'stdout',Path(os.environ['OAP_OUTPUT'])/'stdout.bin')
        assert output.read_bytes().strip()==b'written'
        history=await client.history(conversation['id'])
        assert any(item['body']['role']=='tool' and item['body']['action_id'] for item in history['items'])
        retry=await client.submit(conversation['id'],'Create and publish the result',idempotency_key='work',artifact_ids=(upload['id'],))
        assert retry['id']==run['id'] and retry['reused']
        artifact=result['artifacts'][0]
        path=await client.download(artifact['id'],Path(os.environ['OAP_OUTPUT'])/'artifact.txt')
        assert path.read_text()=='verified artifact'
        print(json.dumps({'run':run['id'],'text':result['text'],'delta':delta,'artifact':artifact['id']}))
asyncio.run(main())
"""
                        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "SystemDrive", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE") if key in os.environ}
                        env.update(PYTHONPATH=str(Path(__file__).parents[1] / "src"), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                                   OAP_URL=origin, OAP_CREDENTIAL=registered["credential"], OAP_BINDING=binding["id"], OAP_OUTPUT=str(root))
                        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c", program, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        output, error = await asyncio.wait_for(process.communicate(), 45)
                        self.assertEqual(process.returncode, 0, error.decode(errors="replace"))
                        result = json.loads(output)
                        self.assertTrue(result["delta"])
                        self.assertEqual(result["text"], final)
                        self.assertEqual(len(requests), 3)
                        self.assertEqual((await host.sessions.list("p"))["items"], [])
                        self.assertFalse((await host.artifacts.list(None, "p"))["items"])
                        self.assertFalse(host.command_sessions.live)
                        self.assertEqual(await host.store.call(lambda db: list(db.execute("PRAGMA foreign_key_check"))), [])
                    finally:
                        if process is not None and process.returncode is None:
                            process.kill()
                            await process.communicate()
                        server.should_exit = True
                        await serving
                        listener.close()
