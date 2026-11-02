# Collaboration

Start with one Agent that can complete its own work. Independent instances provide separate contexts and stable work directories when another responsibility needs them. A session groups work within a project; it grants no authority over another instance.

Collaboration uses persistent boards and private inboxes. A message needs a board, free-form content and an idempotency key. Replies, mentions and evidence references are optional. The host derives the actual sender and work ownership. Once a valid envelope is accepted, an invalid optional recipient or evidence reference does not erase the body: the receipt reports issues and notifications that were not delivered.

Private read grants are explicit. Mentioning someone neither grants access nor widens the audience. Delivery, read cursors and completed work are separate facts. Plain messages create no Run or budget and do not wake every recipient. Directed messages are merged at existing model-input boundaries; child waits use native LangGraph interrupts and dependencies.

Create work through `runs.submit` or an authorized `assign_work`. Continue a wait through its original resume operation. Use `list_members` and `list_boards` to discover reachable identities. Read complete results and artifact references before summarizing them.

The optional manager uses the same capability types as any Agent. Its model and credentials are independent; tools, Skills, workspace and execution inherit the definition unless explicitly narrowed. A role name neither removes business tools nor grants administrator access. Assignment and cancellation require current communication grants and root ownership.

Different instances run concurrently. One instance has one context writer, including a live command retained during a persistent wait. A manager turn may finish while child work continues. Root cancellation follows actual ownership and does not pretend an uncertain external effect stopped.

Discover exact `boards`, `messages`, `instances`, `runs` and `collaboration` contracts through the shared registry. See [SDK](sdk.md) and [module interfaces](collaboration/README.md).
