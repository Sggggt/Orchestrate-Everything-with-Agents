"""Check public source hygiene and local documentation links without reading .git."""

from __future__ import annotations

import re
import sys
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git"}
GENERATED = {
    "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "runtime-data", ".runtime",
    ".oea", ".multi-agent", "env", ".tox", ".nox", ".hypothesis",
    ".ipynb_checkpoints", ".nyc_output", "coverage", "htmlcov", ".cache",
    "test-results", "playwright-report", "blob-report", ".idea", ".vscode",
    "output", "tmp", "temp", "reports",
}
PRIVATE_ROOTS = {
    "data", "state", "workspace", "artifacts", "staging", "backups", "logs",
    "profiles", "screenshots", ".codex", ".claude", ".cursor",
}
RUNTIME_FILES = {
    "client-token", "client.json", "runtime-config.json", "secrets.enc",
    "vault.key", "host.lock",
}
ARTIFACT_PATTERNS = (
    "*.sqlite*", "*.db", "*.db-*", "*.log", "*.log.*", "*.har", "*.pem",
    "*.key", "*.p12", "*.pfx", "*.p8", "*.webm", "*.trace", "*.zip",
    "*.tar", "*.tar.gz", "*.tgz", "*.7z", "*.rar", "*.pyc", "*.pyo",
    "*.bak", "*.orig", "*.rej", "*.local.md", "*.swp", "*.swo", "*~",
)
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".css", ".svg", ".html", ".md", ".toml", ".yaml", ".yml", ".txt", ".ps1", ".cmd"}
PRIVATE = {
    "personal Windows path": re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!Public\b|<)[^\\/\s\"']+", re.I),
    "personal Unix path": re.compile(r"/(?:home|Users)/[a-zA-Z0-9_.-]+/"),
    "private key": re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "provider credential": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{32,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|(?:AKIA|ASIA)[A-Z0-9]{16}|glpat-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})\b"),
    "private service address": re.compile(r"\b(?:https?|ssh|postgres(?:ql)?|mysql|mongodb)://(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?=[:/\s\"']|$)", re.I),
}
CREDENTIAL_URL = re.compile(r"\b(?:https?|ssh|postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s/\"'<>:]+:[^\s/\"'<>@]+@[^\s/\"'<>\x60]+")
INSTANCE_ID = re.compile(r"\b(?:prj|wf|wfr|ses|thr|tsk|run|msg|art)_[0-9a-f]{32}\b", re.I)


def artifact_reason(relative: Path):
    """Identify state even when copied into a directory with an arbitrary name."""
    directories = relative.parts[:-1]
    name = relative.name.lower()
    if (GENERATED.intersection(directories)
            or any(part.startswith((".venv", ".playwright")) for part in directories)
            or relative.parts[0] in PRIVATE_ROOTS):
        return "generated or private directory"
    if name in RUNTIME_FILES or any(
        name.startswith(base + ".") and name.endswith(".new") for base in RUNTIME_FILES
    ):
        return "runtime state or credential file"
    if any(fnmatchcase(name, pattern) for pattern in ARTIFACT_PATTERNS):
        return "generated or private artifact"
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "environment file"
    return None


def content_issues(value: str, *, skill=False):
    """Return categories only, never credential values or private path contents."""
    issues = [label for label, pattern in PRIVATE.items() if pattern.search(value)]
    for match in CREDENTIAL_URL.finditer(value):
        try:
            host = (urlsplit(match.group()).hostname or "").lower()
        except ValueError:
            host = ""
        if host not in {"example.com", "example.org", "example.net"} and not host.endswith((".invalid", ".example")):
            issues.append("credential in service URL")
            break
    if skill and INSTANCE_ID.search(value):
        issues.append("concrete workspace object ID in public skill")
    return issues


def main():
    errors = []
    files = []
    # Prune Git rather than reading even ignored private history or hooks.
    import os
    for directory, children, names in os.walk(ROOT):
        children[:] = [name for name in children if name not in EXCLUDED]
        for name in names:
            files.append(Path(directory) / name)
    for path in files:
        relative = path.relative_to(ROOT)
        reason = artifact_reason(relative)
        if reason:
            errors.append(f"{relative}: {reason}")
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        value = path.read_text(encoding="utf-8-sig")
        for label in content_issues(value, skill=relative.parts[:2] == ("integrations", "codex")):
            errors.append(f"{relative}: {label} (value withheld)")
        if path.suffix == ".md":
            # Exclude fenced code; examples can intentionally contain placeholder links.
            prose = re.sub(r"```.*?```", "", value, flags=re.S)
            targets = re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", prose)
            targets += re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", prose)
            for target in targets:
                target = target.strip().split(' "', 1)[0].strip("<>")
                parsed = urlsplit(target)
                if parsed.scheme or not parsed.path or target.startswith("/"):
                    continue
                resolved = (path.parent / unquote(parsed.path)).resolve()
                if not resolved.is_relative_to(ROOT) or not resolved.exists():
                    errors.append(f"{relative}: broken local link {target}")
    for name in ("README.md", "README.zh-CN.md", "LICENSE", "CONTRIBUTING.md", "SECURITY.md", "docs/README.md"):
        if not (ROOT / name).is_file():
            errors.append(f"Missing {name}")
    logo = ROOT / "src/ma_runtime/web/static/mark.svg"
    skill_logo = ROOT / "integrations/codex/orchestrate-everything-with-agents/assets/mark.svg"
    if not skill_logo.is_file() or logo.read_bytes() != skill_logo.read_bytes():
        errors.append("Skill logo must match the canonical frontend mark")
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Public source checks passed ({len(files)} files; Git history excluded).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
