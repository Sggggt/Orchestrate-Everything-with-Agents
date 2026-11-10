"""Build and stage a platform component outside the checkout, including notices."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native/sandbox"
TARGETS = {"x86_64-pc-windows-msvc":"windows", "x86_64-pc-windows-gnu":"windows", "x86_64-unknown-linux-gnu":"linux"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def outside(path):
    path = path.expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Build output and caches must be outside the source checkout")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", choices=TARGETS, default="x86_64-pc-windows-msvc" if os.name == "nt" else "x86_64-unknown-linux-gnu")
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--binary", type=Path, help="Stage an already built and target-executable component")
    parser.add_argument("--runtime-license", type=Path, action="append", default=[])
    args = parser.parse_args()
    output = outside(args.output)
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if Path(args.cargo).is_absolute():
        env["PATH"] = str(Path(args.cargo).parent) + os.pathsep + env.get("PATH", "")
    env["RUSTUP_TOOLCHAIN"] = tomllib.loads((NATIVE / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    env["CARGO_HOME"] = str(outside(Path(env.get("CARGO_HOME", output / "cargo"))))
    env["CARGO_TARGET_DIR"] = str(output / "target")
    # Panic/source locations in a redistributable binary must not expose the
    # builder's checkout, registry cache, home directory or temporary paths.
    mappings = [(ROOT,"/oea"), (output,"/build"), (Path(env["CARGO_HOME"]),"/cargo"), (Path.home(),"/builder")]
    flags = env.get("CARGO_ENCODED_RUSTFLAGS", "").split("\x1f") if env.get("CARGO_ENCODED_RUSTFLAGS") else []
    flags.extend(f"--remap-path-prefix={source}={target}" for source, target in mappings)
    env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
    manifest = str(NATIVE / "Cargo.toml")
    if args.binary is None:
        subprocess.run([args.cargo,"build","--release","--locked","--target",args.target,"--manifest-path",manifest], check=True, env=env)
    filename = "oea-sandbox.exe" if TARGETS[args.target] == "windows" else "oea-sandbox"
    binary = args.binary.resolve() if args.binary else output / "target" / args.target / "release" / filename
    info = json.loads(subprocess.check_output([str(binary),"--version"], env=env, text=True, encoding="utf-8"))
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if info != {"protocol":1,"version":version,"sdk":"1.0.1","os":TARGETS[args.target],"arch":"x86_64",
                "environment":"msvc" if args.target.endswith("msvc") else "gnu"}:
        raise ValueError("The component does not match this package's version, protocol or target")
    raw = binary.read_bytes()
    for source, _ in mappings:
        for private in (str(source), source.as_posix()):
            if private.encode() in raw or private.encode("utf-16-le") in raw:
                raise ValueError("Rebuild with path remapping before distributing this component")
    metadata = json.loads(subprocess.check_output([args.cargo,"metadata","--locked","--format-version","1",
        "--filter-platform",args.target,"--manifest-path",manifest], env=env, text=True, encoding="utf-8"))
    nodes = {node["id"]:node for node in metadata["resolve"]["nodes"]}
    reachable, pending = set(), [metadata["resolve"]["root"]]
    while pending:
        identity = pending.pop()
        if identity not in reachable:
            reachable.add(identity)
            pending.extend(nodes[identity]["dependencies"])
    asset = output / ("assets-" + args.target + "-" + digest(binary)[:12])
    asset.mkdir(exist_ok=True)
    shutil.copyfile(binary, asset / filename)
    (asset / filename).chmod(0o755)
    notices = []
    def include(source, relative):
        destination = asset / "licenses" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        notices.append({"path":destination.relative_to(asset).as_posix(),"sha256":digest(destination)})
    for package in metadata["packages"]:
        if package["id"] not in reachable or package["name"] == "oea-sandbox":
            continue
        directory = Path(package["manifest_path"]).parent
        sources = [path for path in directory.iterdir() if path.is_file() and path.name.lower().startswith(("license","copying","notice"))]
        if package.get("license_file"):
            source = (directory / package["license_file"]).resolve()
            if not source.is_relative_to(directory.resolve()):
                raise ValueError("A dependency license must be inside its published package")
            sources.append(source)
        if not sources:
            fallback = NATIVE / "licenses" / (package["name"] + "-LICENSE.md")
            if not fallback.is_file():
                raise ValueError(f"Supply the upstream license for {package['name']} {package['version']}")
            sources.append(fallback)
        for source in dict.fromkeys(sources):
            include(source, Path("crates") / (package["name"] + "-" + package["version"]) / source.name)
    rustc = str(Path(args.cargo).with_name("rustc.exe" if os.name == "nt" else "rustc")) if Path(args.cargo).is_absolute() else "rustc"
    sysroot = Path(subprocess.check_output([rustc,"--print","sysroot"], env=env, text=True, encoding="utf-8").strip())
    rust_notices = sysroot / "share/doc/rust"
    include(rust_notices / "COPYRIGHT-library.html", Path("rust/COPYRIGHT-library.html"))
    for source in sorted((rust_notices / "licenses").glob("*")):
        if source.is_file():
            include(source, Path("rust/licenses") / source.name)
    if args.target.endswith("windows-gnu") and not args.runtime_license:
        raise ValueError("GNU Windows distributions need the MinGW and GCC runtime notices")
    for index, source in enumerate(args.runtime_license, 1):
        include(source, Path("runtime") / f"runtime-{index}.txt")
    info.update(binary=filename, sha256=digest(binary), licenses=notices)
    (asset / "manifest.json").write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(asset)


if __name__ == "__main__":
    main()
