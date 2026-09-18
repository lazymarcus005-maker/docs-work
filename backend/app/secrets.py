"""Local secret store for LLM API keys (spec §32.1, §39).

Keys are stored in a single protected file under the data directory,
obfuscated with a per-installation key file. They are write-only through the
API: reads return masked metadata, keys never reach project files, exports,
chat history, or logs. This is deliberate local-app protection, not a
replacement for a hosted secret manager.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path


class SecretStore:
    def __init__(self, secrets_path: Path, key_path: Path) -> None:
        self.secrets_path = Path(secrets_path)
        self.key_path = Path(key_path)
        self._key = self._load_or_create_key()

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return base64.b64decode(self.key_path.read_bytes())
        key = os.urandom(32)
        self.key_path.write_bytes(base64.b64encode(key))
        try:
            os.chmod(self.key_path, 0o600)
            os.chmod(self.secrets_path.parent, 0o700)
        except OSError:
            pass
        return key

    def _crypt(self, data: bytes) -> bytes:
        return bytes(b ^ self._key[i % len(self._key)] for i, b in enumerate(data))

    def _read_all(self) -> dict:
        if not self.secrets_path.exists():
            return {}
        try:
            return json.loads(self._crypt(self.secrets_path.read_bytes()).decode())
        except Exception:
            return {}

    def _write_all(self, data: dict) -> None:
        payload = self._crypt(json.dumps(data).encode())
        self.secrets_path.write_bytes(payload)
        try:
            os.chmod(self.secrets_path, 0o600)
        except OSError:
            pass

    def set(self, ref: str, value: str) -> None:
        data = self._read_all()
        data[ref] = value
        self._write_all(data)

    def get(self, ref: str) -> str | None:
        return self._read_all().get(ref)

    def delete(self, ref: str) -> None:
        data = self._read_all()
        data.pop(ref, None)
        self._write_all(data)


def mask_key(value: str | None) -> str:
    if not value:
        return ""
    return f"{value[:3]}…{value[-3:]}" if len(value) > 8 else "***"
