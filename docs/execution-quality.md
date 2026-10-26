# Delivery and execution evidence

Work acceptance, execution completion, delivery validation and business correctness are separate conclusions. A Run preserves text, structured data, artifacts, usage, errors and the original action receipts. The browser uses short summaries while collaboration can read full evidence.

Delivery requirements are declared in typed configuration and checked at the delivery boundary. Partial results and files survive failure. A model's prose status never overrides observed execution state. Errors identify their source, stage, affected work, continuation state and evidence.

Recovery keeps the original action key and reads its persisted result. Unknown external effects require verification; format preferences in Markdown do not trigger another model call or replay. Context projections retain tool-call pairing and references to original material. See [SDK](sdk.md), [runtime](runtime/README.md), and [context](context/README.md).
