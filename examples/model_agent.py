"""Run one standalone model Agent with explicit external state and project input."""
import argparse
from pathlib import Path
from oea import Agent, Model, ToolKit, Workspace


def main(args):
    with Agent(name="reviewer", model=Model.from_env(),
               instructions="Read the input and deliver a concise, evidenced review.",
               tools=ToolKit.local(files=True), workspace=Workspace(project=Path(args.project)),
               state_dir=Path(args.state_dir)) as agent:
        result = agent.instance("reviewer").run(args.input)
        print(result.text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--input", default="Review the project and identify useful next steps.")
    main(parser.parse_args())
