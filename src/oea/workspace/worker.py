"""Trusted file workload executed inside the command's native OS policy."""

import json
import hashlib
import os
from pathlib import Path
import sys

from oea.contracts.errors import RuntimeFault, require
from oea.workspace.environment import WorkspacePaths
from oea.workspace.patches import apply_patch


def inspect_file(request, paths):
    path = paths.resolve(request["path"])
    try:
        with paths.open_read(str(path)) as source:
            before = os.fstat(source.fileno())
            require(before.st_size <= 50 * 1024 * 1024, "FILE_TOO_LARGE", "Candidate verification is limited to 50 MiB per file")
            fingerprint = hashlib.sha256()
            size = 0
            while chunk := source.read(65536):
                size += len(chunk)
                require(size <= 50 * 1024 * 1024, "FILE_TOO_LARGE", "Candidate verification is limited to 50 MiB per file")
                fingerprint.update(chunk)
            after = os.fstat(source.fileno())
            require(before.st_mtime_ns == after.st_mtime_ns and before.st_size == after.st_size == size,
                    "SOURCE_VERSION_CHANGED", "File changed while its content was being verified")
    except FileNotFoundError:
        return {"path": str(path), "exists": False}
    return {"path": str(path), "exists": True, "bytes": size, "modified_ns": after.st_mtime_ns,
            "sha256": fingerprint.hexdigest()}


def list_outputs(request, paths):
    relative = Path(request["directory"])
    require(not relative.is_absolute() and ".." not in relative.parts, "PERMISSION_DENIED", "Choose a relative output directory")
    directory = paths.resolve(str(paths.output_dir / relative))
    require(directory.is_relative_to(paths.output_dir), "PERMISSION_DENIED", "Candidate listing is limited to this run's output directory")
    if not directory.exists():
        return {"files": [], "directories": [], "cursor": None, "version": None, "truncated": False}
    before = directory.stat()
    version = hashlib.sha256(f"{before.st_dev}:{before.st_ino}:{before.st_mtime_ns}".encode()).hexdigest()
    require(not request.get("version") or request["version"] == version,
            "SOURCE_VERSION_CHANGED", "Output directory changed; read its first page again")
    entries = []
    truncated = False
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if len(entries) == 1000:
                truncated = True
                break
            entries.append(entry)
    entries.sort(key=lambda entry: entry.name)
    offset, limit = request["cursor"], request["limit"]
    page = entries[offset:offset + limit]
    files, directories = [], []
    for entry in page:
        if entry.is_symlink() or Path(entry.path).is_junction():
            continue
        path = Path(entry.path)
        if entry.is_dir(follow_symlinks=False):
            directories.append(path.relative_to(paths.output_dir).as_posix())
        elif entry.is_file(follow_symlinks=False):
            info = entry.stat(follow_symlinks=False)
            files.append({"path": str(path), "name": entry.name, "bytes": info.st_size, "modified_ns": info.st_mtime_ns})
    require(directory.stat().st_mtime_ns == before.st_mtime_ns,
            "SOURCE_VERSION_CHANGED", "Output directory changed while it was being listed")
    return {"files": files, "directories": directories, "version": version, "version_scope": "directory_entries",
            "cursor": offset + len(page) if offset + len(page) < len(entries) else None,
            "truncated": truncated, "entry_limit": 1000}


def main():
    try:
        request = json.loads(sys.stdin.buffer.readline())
        values = request["paths"]
        paths = WorkspacePaths(**{key: tuple(Path(path) for path in value) if isinstance(value, list)
                                  else Path(value) if value is not None else None for key, value in values.items()})
        if request["operation"] == "patch":
            print(json.dumps(apply_patch(request["arguments"], paths), ensure_ascii=False))
        elif request["operation"] in {"list_outputs", "inspect_file"}:
            function = list_outputs if request["operation"] == "list_outputs" else inspect_file
            print(json.dumps(function(request, paths), ensure_ascii=False))
        elif request["operation"] == "read":
            limit = request["max_bytes"]
            with paths.open_read(request["path"]) as source:
                total = 0
                while chunk := source.read(min(65536, limit - total + 1)):
                    total += len(chunk)
                    require(total <= limit, "FILE_TOO_LARGE", "File exceeds the requested byte limit")
                    sys.stdout.buffer.write(chunk)
        else:
            raise RuntimeFault("INVALID_REQUEST", "Unknown internal file operation")
    except RuntimeFault as error:
        print(json.dumps(error.as_dict()), file=sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError) as error:
        print(json.dumps({"code": "FILE_UNAVAILABLE", "safe_message": str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
