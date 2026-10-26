"""Run a project workflow using an existing host, with no model or Agent required.

python -B examples/workflow.py --data-dir EXTERNAL_DATA_DIR --project-id PROJECT_ID
Creates one reusable definition and one run. Keep --run-key to recover an
interrupted run request. No source-repository state or credentials are written.
"""
import argparse
import asyncio
import uuid

from oea import PlatformClient, WorkflowBuilder


def graph():
    return (WorkflowBuilder()
            .node("start", "start", x=60, y=140,
                  input_schema={"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]})
            .node("format", "template", title="整理目标", x=370, y=140,
                  value={"text": "### 工作目标\n\n{{inputs.topic}}"})
            .node("end", "end", x=680, y=140, outputs={"text": "{{nodes.format.text}}"})
            .connect("start", "format").connect("format", "end").build())


async def main(args):
    async with PlatformClient.from_data_dir(args.data_dir) as client:
        workflow = await client.call("workflows.create", {
            "project_id": args.project_id, "name": "Example workflow", "graph": graph().model_dump(mode="json")})
        run = await client.call("workflows.run", {
            "workflow_id": workflow["id"], "inputs": {"topic": args.topic},
            "idempotency_key": args.run_key, "project_id": args.project_id})
        result = await client.call("workflows.result", {
            "workflow_run_id": run["id"], "project_id": args.project_id, "timeout": 10})
        print(result["text"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--topic", default="验证项目内工作流的输入、转换与输出")
    parser.add_argument("--run-key", default="sdk-example-" + uuid.uuid4().hex)
    asyncio.run(main(parser.parse_args()))
