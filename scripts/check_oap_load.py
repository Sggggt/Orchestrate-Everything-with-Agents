"""Exercise OAP over real HTTP with temporary state and native Linux sandboxes."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import socket
import sys
import time

import uvicorn

from oea import AgentDefinition, ApplicationClient, Completion, Model, PlatformClient
from oea.contracts.applications import ApplicationBinding
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one
from oea.web.app import create_app
from oea.workspace.native import spawn


def guard(root):
    memory = {key: int(value.split()[0]) for key, value in
              (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
    assert 1 - memory["MemAvailable"] / memory["MemTotal"] < .8, "Physical memory protection threshold reached"
    assert shutil.disk_usage(root).free >= 5 * 1024**3, "Free disk protection threshold reached"


def percentile(values, fraction):
    return sorted(values)[int((len(values) - 1) * fraction)]


def process_tree(pid):
    pending, found = [pid], set()
    while pending:
        current = pending.pop()
        found.add(current)
        children = Path(f"/proc/{current}/task/{current}/children")
        if children.exists():
            pending.extend(int(value) for value in children.read_text().split() if int(value) not in found)
    return found


class ModelFixture:
    def __init__(self):
        self.calls = 0

    async def complete(self, model, messages, **options):
        self.calls += 1
        return Completion(text="Completed", usage={"prompt_tokens": 1, "completion_tokens": 1})


async def native_stages(root, owned):
    peak = 0
    for count in (1, 4, 8, 20):
        guard(root)
        live, children = [], []
        try:
            for index in range(count):
                work = root / f"native-{count}-{index}"
                work.mkdir()
                code = ("import json,os,pathlib,subprocess,sys,time; "
                        "child=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(60)']); "
                        "pathlib.Path('effect.tmp').write_text(json.dumps([os.getpid(),child.pid])); "
                        "os.replace('effect.tmp','effect'); "
                        "time.sleep(60)")
                process = await spawn({"operation": "exec", "argv": [sys.executable, "-B", "-c", code],
                    "cwd": str(work), "readwrite": [str(work)], "readonly": list({sys.prefix, sys.base_prefix}),
                    "environment": {key: os.environ[key] for key in ("PATH",) if key in os.environ},
                    "network": "deny", "timeout_ms": 90000})
                live.append(process)
                owned.append(process)
            async with asyncio.timeout(20):
                for index in range(count):
                    marker = root / f"native-{count}-{index}" / "effect"
                    while not marker.exists():
                        await asyncio.sleep(.05)
                    assert len(json.loads(marker.read_text())) == 2
                    children.extend(process_tree(live[index].process.pid))
            assert all(process.process.returncode is None and not process.task.done() for process in live)
            peak = max(peak, count)
            await asyncio.sleep(2)
        finally:
            await asyncio.gather(*(process.stop() for process in live))
        assert all(process.terminal["process_stopped"] and process.process.returncode is not None for process in live)
        async with asyncio.timeout(10):
            while any(Path(f"/proc/{pid}").exists() for pid in children):
                await asyncio.sleep(.1)
        print(json.dumps({"native_stage": count, "process_trees_stopped": True}), flush=True)
    return peak


async def denied(awaitable):
    try:
        await awaitable
    except RuntimeFault as error:
        assert error.code in {"PERMISSION_DENIED", "NOT_FOUND"}, error.code
    else:
        raise AssertionError("Cross-application access succeeded")


async def exercise(root, duration, owned):
    fixture = ModelFixture()
    latencies, totals, identities = [], [], set()
    baseline_handles = len(list(Path("/proc/self/fd").iterdir()))
    baseline_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = serving = None
    try:
        async with AsyncExitStack() as stack:
            host = await stack.enter_async_context(Host(root / "state", model_api=fixture))
            await host.projects.create("Example", project_id="example")
            model = await host.model_profiles.save("Fixture", Model(base_url="https://example.invalid", model="fixture"))
            await host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="example", model_ref=model["id"]))
            session = await host.sessions.create("example")
            await host.instances.create(session["id"], "worker")
            origin = f"http://127.0.0.1:{port}"
            server = uvicorn.Server(uvicorn.Config(create_app(host=host, client_url=origin), log_level="error"))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            while not server.started:
                await asyncio.sleep(.02)
            platform = await stack.enter_async_context(PlatformClient.from_data_dir(host.root))
            clients, bindings, conversations = [], [], []
            for name in ("First", "Second"):
                registration = await host.applications.create(name)
                binding = await host.applications.bind(ApplicationBinding(app_id=registration["application"]["id"],
                    project_id="example", agent_id="worker"))
                clients.append(await stack.enter_async_context(ApplicationClient(origin, registration["credential"])))
                bindings.append(binding["id"])
            for index in range(100):
                client = clients[index % 2]
                conversations.append(await client.create_conversation(bindings[index % 2], idempotency_key=f"conversation-{index}"))
            assert len({value["id"] for value in conversations}) == 100
            await denied(clients[1].conversation(conversations[0]["id"]))
            data = b"bounded-transfer" * (1024 * 1024)
            digest = hashlib.sha256(data).hexdigest()
            upload = await clients[0].begin_upload(conversations[0]["id"], "input.bin", len(data), digest, idempotency_key="upload")

            async def chunks():
                for offset in range(0, len(data), 65536):
                    yield data[offset:offset + 65536]
                    await asyncio.sleep(.001)

            await clients[0].upload_content(upload["id"], chunks())
            await clients[0].commit_upload(upload["id"], idempotency_key="commit")
            await clients[0].download(upload["id"], root / "download.bin")
            assert hashlib.sha256((root / "download.bin").read_bytes()).hexdigest() == digest
            response = await clients[1].client.get("/api/oap/v1/artifacts/" + upload["id"] + "/content")
            assert response.status_code == 403
            del data
            started = time.monotonic()

            async def worker(index):
                client, peer = clients[index % 2], clients[1 - index % 2]
                conversation = conversations[index]["id"]
                for sequence in range(100):
                    ordinal = sequence * 100 + index
                    await asyncio.sleep(max(0, started + ordinal * duration / 10000 - time.monotonic()))
                    if sequence % 10 == 0:
                        guard(root)
                    before = time.monotonic()
                    key = f"intent-{ordinal}"
                    run = await client.submit(conversation, "Complete the request", idempotency_key=key)
                    latencies.append(time.monotonic() - before)
                    assert run["id"] not in identities
                    identities.add(run["id"])
                    result = await host.runs.result(run["id"], timeout=30)
                    assert result.text == "Completed"
                    assert (await client.result(run["id"]))["text"] == "Completed"
                    totals.append(time.monotonic() - before)
                    if ordinal % 300 == 0:
                        print(json.dumps({"completed": len(totals), "elapsed_seconds": round(time.monotonic() - started, 1)}), flush=True)
                    if sequence % 10 == 0:
                        replay = await client.submit(conversation, "Complete the request", idempotency_key=key)
                        assert replay["id"] == run["id"] and replay["reused"]
                        await denied(peer.run(run["id"]))
                    if ordinal == 0:
                        stream = client.events(run["id"])
                        event = await anext(stream)
                        await asyncio.sleep(.1)
                        await stream.aclose()
                        position = event.get("position", 0)
                        reconnected = [item async for item in client.events(run["id"], after=position)]
                        positions = [item["position"] for item in reconnected if item["kind"] == "event"]
                        assert positions == sorted(set(positions)) and all(value > position for value in positions)

            async with asyncio.TaskGroup() as workers:
                native = workers.create_task(native_stages(root, owned))
                for index in range(100):
                    workers.create_task(worker(index))
            peak = native.result()
            await asyncio.sleep(max(0, started + duration - time.monotonic()))
            elapsed = time.monotonic() - started
            counts = await host.store.call(lambda db: {
                "runs": one(db, "SELECT COUNT(*) AS n FROM runs")["n"],
                "completed": one(db, "SELECT COUNT(*) AS n FROM runs WHERE state='completed'")["n"],
                "charges": one(db, "SELECT COUNT(*) AS n FROM usage WHERE kind='model' AND state='settled'")["n"]})
            assert counts == {"runs": 10000, "completed": 10000, "charges": 10000}, counts
            assert fixture.calls == len(identities) == 10000
            assert peak == 20 and elapsed >= 1800
            assert percentile(latencies, .95) < 5 and percentile(totals, .95) < 30
            assert (await platform.call("runs.list", {"project_id": "example"}))["items"] == []
            assert (await platform.call("artifacts.list", {"project_id": "example"}))["items"] == []
            assert not host.active and not host.command_sessions.live
            server.should_exit = True
            await serving
            serving = None
        assert len(list(Path("/proc/self/fd").iterdir())) <= baseline_handles + 64
        assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - baseline_memory < 1024 * 1024
        print(json.dumps({"submissions": len(identities), "conversations": 100, "native_peak": peak,
            "seconds": round(elapsed, 2), "throughput": len(identities) / elapsed,
            "submit_p95": percentile(latencies, .95), "result_p95": percentile(totals, .95),
            "foreign_reads": 0, "duplicate_runs_or_charges": 0, "lost_receipts": 0}))
    finally:
        if serving is not None:
            server.should_exit = True
            await serving
        listener.close()
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=1800)
    args = parser.parse_args()
    root = args.work_dir.resolve()
    source = Path(__file__).resolve().parents[1]
    assert sys.platform == "linux", "Run on the native Linux CI worker"
    assert args.duration >= 1800, "Acceptance requires at least 30 minutes"
    assert args.work_dir.is_absolute() and not root.exists() and not root.is_relative_to(source)
    root.mkdir(parents=True)
    owned = []
    try:
        guard(root)
        asyncio.run(exercise(root, args.duration, owned))
    finally:
        assert all(process.process.returncode is not None and process.terminal and process.terminal["process_stopped"]
                   for process in owned), "Retain test state until native processes have stopped"
        assert root.is_relative_to(args.work_dir.parent.resolve()) and root != args.work_dir.parent.resolve()
        shutil.rmtree(root)
    assert not root.exists()


if __name__ == "__main__":
    main()
