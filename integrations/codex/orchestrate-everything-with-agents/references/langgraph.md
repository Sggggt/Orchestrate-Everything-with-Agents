# Python code graphs

Register a trusted graph factory through `host.register_graph(name, factory, version=...)`. Compile with the exact SQLite checkpointer supplied to the factory. Select it through `Execution(graph=name, graph_version=version)`.

Use the shared nodes and state contracts in `oea.orchestration.graphs`. LangGraph owns transitions, native interrupts and graph checkpoints. Runtime clients enter through graph context; keep clients, credentials, full histories and binary data out of checkpoint state.

Deterministic transformations belong in ordinary nodes. Side effects belong in authorized tools with stable action keys. Recovery reads original receipts before continuing. Re-register the saved graph version before reopening a host that must recover it. Exposed topology describes the actual graph and is maintained in code.

See the public SDK and orchestration module interfaces for the current factory and node contracts.
