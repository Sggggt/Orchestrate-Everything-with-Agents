"""Call a granted Agent through an existing local Host's OAP endpoint."""

import argparse
import asyncio
import os

from oea import ApplicationClient


async def run(options):
    async with ApplicationClient(os.environ["OEA_URL"], os.environ["OAP_CREDENTIAL"]) as client:
        conversation = await client.create_conversation(os.environ["OAP_BINDING_ID"], idempotency_key=options.conversation_key)
        handle = await client.submit(conversation["id"], options.input, idempotency_key=options.run_key)
        print("Accepted Run:", handle["id"])
        async for event in client.events(handle["id"]):
            if event["kind"] == "run.completed":
                print(event["result"]["text"])
            elif event["kind"] in {"run.waiting", "run.failed", "run.cancelled", "run.needs_review", "error"}:
                print(event)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--conversation-key", required=True)
    parser.add_argument("--run-key", required=True)
    asyncio.run(run(parser.parse_args()))
