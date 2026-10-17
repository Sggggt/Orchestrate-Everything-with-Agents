"""Synthetic scheduler benchmark. All state and reports stay outside the checkout."""

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from oea import AdapterEvent, AgentDescriptor, Runtime, RuntimeConfig, TaskResult, TaskSpec, WorkflowBuilder
from ma_runtime.paths import external_path


class Worker:
    def __init__(self, identity, concurrency, delay, metrics):
        self.identity, self.concurrency, self.delay, self.metrics = identity, concurrency, delay, metrics
        self.active = self.peak = self.completed = 0

    def describe(self):
        return AgentDescriptor(agent_id=self.identity, max_concurrency=self.concurrency)

    async def execute(self, request, context):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.metrics["active"] += 1
        self.metrics["peak"] = max(self.metrics["peak"], self.metrics["active"])
        try:
            await asyncio.sleep(self.delay)
            self.completed += 1
            self.metrics["latencies"].append(time.perf_counter() - self.metrics["started"])
            yield AdapterEvent(type="completed", result=TaskResult(output=request.payload))
        finally:
            self.active -= 1
            self.metrics["active"] -= 1

    async def aclose(self):
        pass


async def sample(args):
    metrics = {"active": 0, "peak": 0, "latencies": []}
    workers = {f"worker-{index}": Worker(f"worker-{index}", args.agent_concurrency, args.delay, metrics)
               for index in range(args.agents)}
    identities = list(workers)
    with tempfile.TemporaryDirectory(prefix="scheduler-", dir=external_path(args.data_dir)) as directory:
        async with Runtime.open(directory, RuntimeConfig(max_concurrency=args.concurrency,
                                reserved_external_slots=0), adapters=workers) as runtime:
            session = await runtime.sessions.create()
            started = time.perf_counter()
            metrics["started"] = started
            cpu_started = time.process_time()
            handles = []
            if args.mode == "tasks":
                for number in range(args.tasks):
                    identity = identities[number % len(identities)]
                    thread = await runtime.threads.create(session.id, identity)
                    handles.append(await runtime.tasks.submit(TaskSpec(session_id=session.id,
                        thread_id=thread.id, agent_id=identity, payload={"number": number},
                        idempotency_key=f"item-{number}")))
            else:
                if args.mode == "workflow":
                    graph = (WorkflowBuilder().node("start", "start")
                        .node("map", "map", agent_ids=identities, max_concurrency=args.concurrency,
                              payload={"number": "{{item}}"}, routing="least_loaded")
                        .node("end", "end", outputs={"items": "{{nodes.map.outputs}}"})
                        .connect("start", "map").connect("map", "end").build())
                    workflow = await runtime.workflows.create("default", "Synthetic map", "definition", graph=graph)
                for offset in range(0, args.tasks, 128):
                    numbers = list(range(offset, min(offset + 128, args.tasks)))
                    if args.mode == "instances":
                        handles.append(await runtime.instances.spawn({"session_id": session.id,
                            "agent_ids": identities, "payloads": [{"number": n} for n in numbers],
                            "idempotency_key": f"batch-{offset}", "max_concurrency": args.concurrency,
                            "routing": "least_loaded"}))
                    else:
                        handles.append(await runtime.workflows.run(workflow.id, {"items": numbers}, f"run-{offset}"))
            submitted = time.perf_counter()
            if args.mode == "tasks":
                results = await asyncio.gather(*(runtime.tasks.result(h.id, timeout=120) for h in handles))
                outputs = [result.output for result in results]
            elif args.mode == "instances":
                results = await asyncio.gather(*(runtime.instances.result(h["id"], timeout=120) for h in handles))
                outputs = [member["result"]["output"] for result in results for member in result["instances"]]
            else:
                results = await asyncio.gather(*(runtime.workflows.result(h.id, timeout=30) for h in handles))
                outputs = [item for result in results for item in result["outputs"]["items"]]
            elapsed = time.perf_counter() - started
            assert sum(worker.completed for worker in workers.values()) == args.tasks
            assert [output["number"] for output in outputs] == list(range(args.tasks))
            assert metrics["peak"] <= min(args.concurrency, args.agent_concurrency * args.agents)
            assert all(worker.peak <= args.agent_concurrency for worker in workers.values())
            latencies = sorted(metrics["latencies"])
            return {"seconds": round(elapsed, 4), "submit_seconds": round(submitted - started, 4),
                    "cpu_seconds": round(time.process_time() - cpu_started, 4),
                    "p50_completion_seconds": round(statistics.median(latencies), 4),
                    "p95_completion_seconds": round(latencies[min(len(latencies)-1, int(len(latencies)*0.95))], 4),
                    "tasks_per_second": round(args.tasks / elapsed, 2), "peak_active": metrics["peak"],
                    "per_agent_peak": {identity: worker.peak for identity, worker in workers.items()}}


async def main(args):
    root = external_path(args.data_dir)
    root.mkdir(parents=True, exist_ok=True)
    samples = [await sample(args) for _ in range(args.repeats)]
    report = {"mode": args.mode, "tasks": args.tasks, "concurrency": args.concurrency, "agents": args.agents,
              "agent_concurrency": args.agent_concurrency, "delay_seconds": args.delay,
              "samples": samples, "median_tasks_per_second": statistics.median(
                  result["tasks_per_second"] for result in samples)}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--tasks", type=int, default=128)
    parser.add_argument("--mode", choices=("tasks", "instances", "workflow"), default="instances")
    parser.add_argument("--agents", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--agent-concurrency", type=int, default=4)
    parser.add_argument("--delay", type=float, default=0.03)
    parser.add_argument("--repeats", type=int, default=3)
    options = parser.parse_args()
    if not 1 <= options.tasks <= 1000 or not 1 <= options.agents <= 32 or options.repeats < 1 or options.delay < 0:
        parser.error("Use 1..1000 tasks, positive repeats and a nonnegative delay")
    asyncio.run(main(options))
