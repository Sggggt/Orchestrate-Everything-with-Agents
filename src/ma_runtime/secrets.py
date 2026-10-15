"""Local write-only credential vault, never part of task or agent snapshots."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

from .errors import RuntimeFault


def atomic_write(path: Path, data: bytes):
    temporary = path.with_suffix(path.suffix + "." + uuid.uuid4().hex + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def dpapi(data: bytes, decrypt=False) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    source_buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeFault(
            "SECRET_UNAVAILABLE",
            "Current user cannot access the local credential vault",
        )
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


class SecretVault:
    def __init__(self, root: Path):
        self.path = root / "secrets.enc"
        self.lock = threading.RLock()
        self.fernet = None
        if os.name != "nt":
            key_path = root / "vault.key"
            if not key_path.exists():
                atomic_write(key_path, Fernet.generate_key())
            os.chmod(key_path, 0o600)
            self.fernet = Fernet(key_path.read_bytes())
        self.values = {}
        if self.path.exists():
            try:
                raw = self.path.read_bytes()
                decoded = (
                    dpapi(raw, decrypt=True)
                    if os.name == "nt"
                    else self.fernet.decrypt(raw)
                )
                self.values = json.loads(decoded)
            except Exception:
                raise RuntimeFault(
                    "SECRET_UNAVAILABLE", "Cannot decrypt the local credential vault"
                ) from None
        self.known_secrets = set(self.values.values())

    def get(self, agent_id: str) -> str:
        return self.values.get(agent_id, "")

    def set(self, agent_id: str, value: str):
        with self.lock:
            changed = dict(self.values)
            if value:
                changed[agent_id] = value
            else:
                changed.pop(agent_id, None)
            raw = json.dumps(changed).encode()
            encoded = dpapi(raw) if os.name == "nt" else self.fernet.encrypt(raw)
            atomic_write(self.path, encoded)
            self.values = changed
            if value:
                self.known_secrets.add(value)

    def redact(self, value):
        if isinstance(value, str):
            for secret in tuple(self.known_secrets):
                if secret:
                    if len(secret) >= 8:
                        value = value.replace(secret, "[redacted]")
                    elif value == secret:
                        value = "[redacted]"
            return value
        if isinstance(value, dict):
            return {
                k: (
                    "[redacted]"
                    if k.lower()
                    in {"api_key", "authorization", "password", "access_token"}
                    else (
                        v
                        if k
                        in {
                            "schema_version",
                            "protocol_version",
                            "adapter_version",
                            "format_version",
                        }
                        else self.redact(v)
                    )
                )
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        return value
