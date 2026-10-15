"""Run a project workflow using an existing host, with no model or Agent required.

python -B examples/workflow.py --data-dir EXTERNAL_DATA_DIR --project-id PROJECT_ID
Creates one reusable definition and one run; keys can be supplied to recover an
interrupted client request. No source-repository state or credentials are written.
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
                  template="### 工作目标\n\n{{inputs.topic}}")
            .node("end", "end", x=680, y=140, outputs={"text": "{{nodes.format.text}}"})
            .connect("start", "format").connect("format", "end").build())


async def main(args):
    async with PlatformClient.from_data_dir(args.data_dir) as client:
        workflow = await client.workflows.create(args.project_id, "SDK 工作流示例", args.creation_key, graph=graph())
        run = await client.workflows.run(workflow["id"], {"topic": args.topic}, args.run_key,
                                         expected_revision=workflow["revision"])
        print("workflow_id:", workflow["id"], "run_id:", run["id"])
        result = await client.workflows.result(run["id"], timeout=10)
        print(result["outputs"]["text"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--topic", default="验证项目内工作流的输入、转换与输出")
    parser.add_argument("--creation-key", default="sdk-workflow-example-v1")
    parser.add_argument("--run-key", default="sdk-example-" + uuid.uuid4().hex)
    asyncio.run(main(parser.parse_args()))
