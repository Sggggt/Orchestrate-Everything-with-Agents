# Local work capabilities

An Agent's Workspace defines project input, work and output directories. Project input is read-only by default. File tools, local commands and process Agents receive the same resolved paths. Work and per-Run outputs live outside the source repository unless explicitly configured.

`ToolKit.local(files=True, commands=True)` selects local capabilities for an owned standalone Agent. An existing host must also explicitly enable commands with `--allow-commands`. Programs run with the host user's permissions; managed processes are not an operating-system sandbox.

File tools enforce path grants, link boundaries and revision checks. Explicitly shared files use host-side canonical-path checks; shell programs and external editors remain responsible for their own coordination. Images and artifacts preserve original source, hashes and ownership rather than embedding full data in graph state.

Use saved external language environments and explicit executable arguments. Configuration checks preview paths and program availability without running a model or preparing business work. See [SDK](sdk.md), [workspace interfaces](workspace/README.md), [tools](tools/README.md), and [process Agents](stdio/README.md).
