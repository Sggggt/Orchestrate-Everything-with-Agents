# Applications and OAP

OAP 1.0 is available at `/api/oap/v1` for local application backends. Its
`ApplicationClient` connects to an existing Host. The normal control protocol
remains 3.0; the product release introducing OAP is 3.3.1.

The applications module owns external application registrations, credentials,
Agent bindings and execution scope. OAP (OEA Application Protocol) has its own
version, independent of the OEA product, control protocol and state schema.

An application manages its own code, processes, user interface and business
orchestration. OEA supplies authorized Agent execution through the existing
Host, LangGraph, cost ledger, action receipts and native sandbox.

## Management

The shared control registry exposes `applications.create`, `get`, `list`,
`rotate_credential`, `set_enabled`, `remove`, `bind` and `bindings`. Management
requires the normal platform identity. Application credentials do not authorize
control operations.

Creating a registration or rotating its credential returns a bearer credential
once. Only a verifier is persisted. Rotation immediately invalidates the old
credential without changing the application identity. Disabling or removing a
registration preserves its historical execution facts.

A binding names one saved Agent and one source project, including for global
Agents. Its `ApplicationGrant` explicitly limits tools, Skills, directories,
network access, environment and cost. The effective capabilities are the
intersection with the source Agent's current permissions. Application grants
cannot inherit a project workspace or choose a managed instance's directory.
Trusted in-process extensions require separate explicit authorization.

The browser's **Application access / 应用接入** page manages registrations,
credential rotation and Agent grants. It shows configuration, without loading
application conversations. A binding's application, project and Agent are
immutable; create another binding to change them. Narrowing a grant applies to
subsequent capability calls and stops affected live command environments.

Applications do not receive platform member lists, boards, inboxes, messages,
manager roles or delegation. These capabilities are absent from discovery and
rejected at execution boundaries. MCP connections use application-instance
identities even when the source service normally uses a shared connection.
Custom programs, Python graphs, graph functions and in-process tools need
`trusted_extensions=True`; they execute trusted host code and are not an OS
sandbox for that extension. Default local tools still require the native sandbox.

## Call an Agent

First register an application and grant an existing Agent through the management
SDK, control MCP or browser. Keep the returned credential in the application's
secret store. Use the granted binding ID to create a conversation:

```python
import asyncio
import os
from oea import ApplicationClient

async def main():
    async with ApplicationClient(
        os.environ["OEA_URL"], os.environ["OAP_CREDENTIAL"]
    ) as client:
        bindings = await client.agents()
        conversation = await client.create_conversation(
            bindings["items"][0]["id"], idempotency_key="conversation-001"
        )
        run = await client.submit(
            conversation["id"], "Explain the supplied requirements.",
            idempotency_key="work-001"
        )
        async for event in client.events(run["id"]):
            if event["kind"] == "run.completed":
                print(event["result"]["text"])
            elif event["kind"] in {"run.waiting", "run.failed", "run.needs_review"}:
                print(await client.run(run["id"]))

asyncio.run(main())
```

Keep each accepted intent's key. Retrying the same operation, target and key
returns its original receipt; changed parameters conflict. A disconnected SSE
stream does not cancel work. Resume a waiting Run with `client.resume`, its
original Run ID and a new stable reply key. Cancellation reports observed stop
state and does not promise to undo an external effect. Closing a conversation
rejects new work and returns references to unfinished Runs; history is retained.

## HTTP contract

Send `Authorization: Bearer <application-credential>` and `X-OAP-Version: 1.0`.
Cookies and management credentials do not authenticate OAP. The authenticated
application identity is injected by the Host; request bodies cannot select it.
`X-Request-ID` is optional and is echoed in the JSON envelope. Mutating JSON
requests use `Idempotency-Key`. JSON responses contain `protocol_version`,
`request_id`, and either `result` or the shared safe error fields.

`GET /capabilities` reports implemented operations, limits and native component
inspection. `/schema/conversation`, `/schema/run`, `/schema/resume`, `/schema/resolution` and
`/schema/upload` expose schemas generated from the request types. These paths
are relative to `/api/oap/v1`.

| Route | Purpose |
| --- | --- |
| `GET /agents` | Page through authorized bindings |
| `POST /conversations`, `GET /conversations` | Create or page through owned conversations |
| `GET /conversations/{id}`, `GET /conversations/{id}/history` | Read state, original caller/model messages and tool receipt references |
| `POST /conversations/{id}/runs` | Accept work on its dedicated instance |
| `POST /conversations/{id}/close` | Close new acceptance without deleting history |
| `GET /runs/{id}`, `GET /runs/{id}/result` | Read state, usage and the complete original result |
| `GET /runs/{id}/events` | Stream durable facts and transient model deltas |
| `GET /runs/{id}/response` | Recover original partial or completed model text by attempt, version and character offset |
| `GET /runs/{id}/actions`, `GET /runs/{id}/actions/{action_id}` | Read original action metadata and business results, optionally selecting a JSON Pointer |
| `POST /runs/{id}/actions/{action_id}/confirm` | Record external verification of an owned unresolved effect without executing it |
| `POST /runs/{id}/resume`, `POST /runs/{id}/cancel` | Continue or cancel the original work |
| `GET /runs/{id}/commands/{command_id}` | Read retained stdout/stderr by byte offset |
| `GET /runs/{id}/commands/{command_id}/output/{stream}` | Download retained stdout/stderr bytes, optionally selecting `offset`/`limit` |
| `GET /runs/{id}/artifacts`, `GET /runs/{id}/candidates` | Page through published files and saved patch candidates |
| `POST /uploads`, `GET /uploads/{id}` | Accept file metadata or inspect its receipt |
| `PUT /uploads/{id}/content`, `POST /uploads/{id}/commit` | Stream bytes and reconcile completed publication |
| `GET /artifacts/{id}/content` | Download an authorized immutable file, with HTTP Range support |

List endpoints use bounded `cursor`/`limit` pages. History uses `after`/`limit`;
retain its returned `version` while paging through a fixed history boundary.
Tool messages reference their original actions. Private runtime instructions,
model requests and internal command environment metadata are excluded.
SSE reconnects accept `after` or `Last-Event-ID` for durable event positions.
Model deltas retain original attempt, block and sequence identities; a resync
notification directs the client back to the retained result rather than
re-executing an action. Rotation or disablement also ends an active stream's
authorization. An application cannot read another application's Run or a
normal platform Run, including by guessing an ID.

Use `response` to fill a model-stream gap by its original attempt and version,
and `result` for final delivery. `download_command` reads the retained binary
stdout/stderr directly, without forcing repeated inline-text pages or executing
the command again. Its response headers identify the captured byte boundary and
observed command state.

For an unresolved effect, the application backend first checks the original
action, retained command output and actual external state. `confirm_action`
records concrete evidence and either the verified original result (`completed`)
or a verified absence of effects (`not_applied`). It reuses the core confirmation
ledger and does not execute, retry or roll back the action. Verification is
reported separately from the original native stop observation. Resume explicitly
after confirmation; partial effects must not be declared `not_applied`. Model
request bodies and private memory-generation results are not exposed as action
results; use `response` for the original user-visible model output.

Text inputs are limited to 1 MiB, JSON requests to 2 MiB and individual files to
50 MiB, all measured in bytes. Uploads declare size and SHA-256 before receiving
content. Only complete matching bytes are published. A content upload uses its
accepted upload ID; a lost commit response is reconciled through the same ID.
Use `upload_status`, `commit_upload` and `download` on `ApplicationClient`.
Downloads return the full-file SHA-256 in `X-Content-SHA256`; the SDK verifies it.

## State ownership

An application conversation has one immutable binding and one dedicated session.
Its first accepted work creates a dedicated instance. Subsequent work reuses that
instance; a different conversation cannot inherit its context. Runs and their
budgets, commands, artifacts and checkpoints retain the existing ownership chain.

Application instances use a host-owned directory under
`application-workspaces/<application>/<binding>/<conversation>/instances/`.
Their `work/outputs/<run>` and sibling `temp/` stay within that ownership tree.
Assigned files are copied from immutable artifacts into the conversation's
read-only `inputs/<artifact>/<name>` directory. Applications never inherit the
normal project's folder, environment or AGENT.md. Explicitly granted references,
environment values and Skills still intersect with the source Agent's grants.
Windows work directories must fit the native process working-directory limit;
use a short external state root. Win32 profile variables can use an existing DOS
name for the same private temporary directory. This creates no directory link
and grants no additional filesystem scope.
Published artifacts retain the common immutable artifact store and are authorized
by their recorded conversation and Run, not by file names or caller-supplied IDs.

The OAP schema transition is an explicit offline operation. Use `oea upgrade-state`
against stopped state, first as a dry run and then with a new target directory.
Recognized pre-OAP protocol-3 state retains its original IDs, absolute workspace
bindings and graph checkpoints. Earlier protocol transitions retain their own
documented recovery restrictions. The source is never rewritten in place.
This transition rotates the local control credential in the upgraded target;
model credentials remain unchanged. Connect clients to that target and reopen
the browser through `oea open` or the Windows launcher.

Normal platform SDK, control MCP, workbench lists, search, unread counts, events,
downloads, candidates and result references exclude application business
objects. Default architecture changes affect only subsequently accepted work; active application Runs retain their original graph. Management task history and user-handling operations exclude application business records. Removing a source
project follows the ordinary project deletion contract and revokes its bindings.
