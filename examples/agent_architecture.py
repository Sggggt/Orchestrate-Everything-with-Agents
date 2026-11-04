"""Publish and try an editable architecture on an explicitly selected instance."""

import argparse
import asyncio
import json

from oea import AgentArchitecture, ArchitectureBinding, ArchitectureBuilder, ArchitectureClient, PlatformClient, RunSpec


async def main(args):
    async with PlatformClient.from_data_dir(args.data_dir) as client:
        architectures = ArchitectureClient(client)
        template = await architectures.templates(args.template)
        graph = ArchitectureBuilder(template["graph"]).build(max_steps=256)
        draft = await architectures.create(AgentArchitecture(
            id=args.architecture, project_id=args.project, name=args.template,
        ), graph)
        validation = await architectures.validate(args.architecture, args.project, expected_revision=draft["revision"])
        if not validation["valid"]:
            print(json.dumps(validation, ensure_ascii=False, indent=2))
            return
        published = await architectures.publish(args.architecture, args.project, draft["revision"])
        binding = ArchitectureBinding(architecture_id=args.architecture, version=published["version"])
        run = await architectures.run(args.project, binding, RunSpec(
            instance_id=args.instance, input=args.goal, idempotency_key=args.key,
        ))
        result = await client.call("runs.result", {"run_id": run["id"], "timeout": 300})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.bind:
            agent = await client.call("agents.get", {"agent_id": args.agent})
            await architectures.bind(args.agent, args.project, binding, agent["revision"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", help="Optional existing host state directory")
    for name in ("project", "agent", "instance", "architecture", "goal", "key"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--template", choices=("react", "plan_execute", "reflexion"), default="react")
    parser.add_argument("--bind", action="store_true", help="Apply the candidate after a successful trial")
    asyncio.run(main(parser.parse_args()))
