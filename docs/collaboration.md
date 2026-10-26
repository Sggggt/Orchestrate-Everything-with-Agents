# Collaboration

Agent definitions configure reusable capabilities; Agent instances provide stable identities, separate contexts and working directories. Sessions group collaboration within a project. Sharing a session does not grant authority over another instance.

Direct mode executes the instance's configured capabilities. Collaborative mode explicitly enables a separately configured manager with its own model, secrets and communication scope. Managers coordinate within root authority and budget; they do not inherit instance business commands or provider credentials.

Messages have four purposes: `inform` adds evidence to an inbox, `request` creates explicitly authorized work, `reply` continues an original wait, and `report` records facts or issues. Delivery, inbox receipt, application, work completion and user read cursors remain separate observations. Text mentions do not create authority or implicitly start all recipients.

Independent instances run concurrently, while an instance has one context writer. Child work uses real dependency receipts and native LangGraph interrupts. Complete results and immutable artifact references carry evidence between participants. A root cancellation follows actual ownership; an ended manager turn does not automatically cancel instances.

Use the current `messages`, `instances`, `runs`, and `collaboration` operation contracts. See [SDK](sdk.md) and [collaboration module](collaboration/README.md).
