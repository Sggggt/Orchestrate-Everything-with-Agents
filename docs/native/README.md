# Native execution component

The Python host owns authorization, sessions, budgets and receipts. A private Rust component owns one process tree through **MXC Rust typed V1 SDK 1.0.1**. LangGraph remains the execution engine. The component is not a public CLI, daemon or separate planner.

| Platform | Backend | Requirements |
| --- | --- | --- |
| Windows x64 | ProcessContainer/PSEC | Windows 11 with the required native APIs and policy capabilities; MSVC builds also require the Visual C++ v14 x64 runtime |
| Linux x64 | Bubblewrap | Compatible C runtime, `bwrap` 0.5+, permitted unprivileged user/network namespaces and process descriptors |

Windows rejects AppContainer/DACL augmentation and other fallback tiers. It creates no dedicated account and changes no global ACL or firewall policy. Linux does not fall back to another backend. Unsupported dependencies or policies fail explicitly.

For MSVC-built Windows wheels, install the [Visual C++ runtime prerequisite](../getting-started.md#windows-runtime) before using native tools. The component imports `VCRUNTIME140.dll`; this DLL is supplied by Microsoft's redistributable, not by the OEA wheel. A successful Python package installation alone does not verify that the native component can load.

## Execution boundary

Commands, process Agents, patch workers and file/image capture use one effective policy. Workloads receive approved material, private work/output/temp directories and read-only runtimes. Model credentials and controller tokens remain with the host. An authorized host broker may transfer an assigned immutable artifact without opening an arbitrary workload path.

Business streams use pipes. An authenticated, short-lived control connection carries version, start and terminal facts separately. Workload output cannot impersonate a terminal receipt. Yielding keeps the original process. Owner loss is unknown even when the OS subsequently cleans up the process.

Both backends report `pty=false`. Windows CLI libraries may use Win32k inside the PSEC container UI namespace. External UI objects, clipboard access, input injection, desktop switching and system/display changes remain denied. This does not provide host desktop automation.

Filesystem isolation is a process boundary, not a separate virtual disk. Explicit shares and pre-existing aliases can expose the same underlying object. Patch operations additionally reject linked-file writes.

## Networking and inspection

`deny` requests isolated workload networking. `allow` permits outbound traffic. Windows also applies the selected inbound and host-loopback policy when supported. The current Linux SDK retains private inbound networking and denies host loopback even with outbound allowance; capabilities report this restriction.

Linux outbound networking also needs `slirp4netns`, `ip`, `nsenter`, `iptables`, `ip6tables` and their restore tools. The SDK applies rules in private namespaces. Installation must not modify global sysctl, AppArmor or firewall policy to make a request pass. DNS caches and resolver behavior remain distinct from actual socket traffic.

Host security policy can deny capabilities inside unprivileged namespaces even when namespace creation succeeds. Ubuntu 24.04 and later include AppArmor restrictions in this area. A namespace or loopback-setup permission failure leaves native execution unavailable; OEA does not disable the host's security policy. See [Ubuntu's namespace restriction documentation](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/).

`host.status` and `agents.check` report component/backend inspection, cached by component revision and host PATH. Inspection does not prove business correctness or replace execution testing. The SDK validates every launch, including Windows' native-only selection.

## Build and package

Release wheels contain only the matching component, manifest and redistribution notices. End users need no Rust, Node or .NET runtime for this component. Linux system dependencies remain prerequisites. Local Linux wheels use the build host's C runtime and are not advertised as universal manylinux wheels.

Source builds use the pinned toolchain and `Cargo.lock`. Choose an external output directory:

```sh
python -B scripts/build_native.py --output /path/to/external-build
```

Windows source builds need Rust's MSVC toolchain and C++/Windows SDK build dependencies. GNU cross-builds are supported with the MinGW and GCC notices supplied through `--runtime-license`. Compiler choice does not change the PSEC backend.

Set `OEA_NATIVE_ASSETS` to the generated `assets-...` directory, then build the wheel outside the checkout. The hook includes only manifest-listed, hash-verified assets matching OS, architecture, SDK, protocol and version. Source archives include Rust source and the lock file, never binaries or target directories. Release builds remap private source/cache paths.

Run the release workflow manually for the intended commit or version tag. It builds on Windows and Ubuntu 22.04, checks the exact archives and installs each wheel into a separate environment. For a version tag, it also prepares a draft release with both wheels, the source archive and SHA-256 checksums. Verify the required native backends before publishing the draft. To check a downloaded wheel locally, run `python -B scripts/check_wheel_install.py --wheel /path/to/package.whl --work-dir /path/to/new-check-directory --native`; the directory must be new and outside the checkout.

Editable installation without assets supports model/MCP development; native local tools remain unavailable and do not silently become trusted. See [installation](../getting-started.md).

Upstream: [MXC 1.0.1](https://github.com/microsoft/mxc/tree/v1.0.1), [typed SDK](https://github.com/microsoft/mxc/blob/v1.0.1/src/mxc-sdk/README.md), [Windows support](https://github.com/microsoft/mxc/blob/v1.0.1/docs/backends/process-container/os-version-support.md).
