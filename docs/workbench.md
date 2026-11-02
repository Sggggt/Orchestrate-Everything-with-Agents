# Local work capabilities

An Agent uses one execution environment for commands, patches, publication and image reads. Projects supply directory, Shell and network defaults; Agents can override them or disable inheritance. Each instance has a stable private work directory and each Run has an output directory. Explicitly shared directories share their underlying files.

| Tool | Behavior |
| --- | --- |
| `exec_command` | Execute argv or Shell text; use programs for reading, searching and batch work. |
| `write_stdin` | Read saved output, provide input, close input or terminate the original command session. |
| `apply_patch` | Add, update or delete a granted file; return context/revision conflicts to the same Agent. |
| `view_image` | Capture a granted image or read an assigned immutable image artifact. |
| `publish_artifact` | Publish selected bytes with actual Run ownership and a content hash. |

`ToolKit.local(files=True, commands=True, images=True)` selects these capabilities. An existing host must also enable commands with `--allow-commands`. The default `Workspace(sandbox="native")` uses packaged MXC: Bubblewrap on Linux and native ProcessContainer/PSEC on Windows. Missing capabilities fail explicitly. `sandbox="trusted"` explicitly uses host-user authority and provides no OS isolation.

A short yield preserves the running process. Continue with its `command_session_id`; do not rerun a command to recover output. Retention, inline output size and execution timeout are separate settings. Saved output reports byte positions and truncation. Cancellation requests and confirmed stops remain separate facts. Lost ownership remains unknown until the original evidence is checked.

Project input and references are read-only. `writable_project_paths` and `writable_directories` grant selected writes, including project-external directories. Host state and credentials cannot be exposed as material. Runtimes are shared read-only; workload environments do not inherit model or controller credentials.

Both native backends currently use pipes; `tty=true` returns `CAPABILITY_UNAVAILABLE`. Linux outbound allowance retains private inbound networking and denies host loopback. Windows enforces the selected inbound/loopback policy. See [native support](native/README.md).

Natural language, Markdown and files are normal results. Strict schemas, minimum artifact counts and required tool evidence are opt-in. See [SDK](sdk.md), [workspace](workspace/README.md), [tools](tools/README.md) and [process Agents](stdio/README.md).

Run details include **文件候选** for unpublished files. Choose patch receipts or the run's output directory, then verify an individual file's current content version. Changed or missing sources retain the original observation and prompt review of conclusions that used that version. Directory browsing is bounded and does not publish files or create work.
