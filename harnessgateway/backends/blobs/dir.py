"""Blobs in a directory, with signed URLs the API serves itself: the one-box deployment."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import shutil
import time
from pathlib import Path

from ...errors import Invalid, NotFound
from . import checked_key


class DirBlobs:
    """Blobs in a directory. Its presigned URLs point at the API's blob route, signed with `secret`."""

    def __init__(self, root: Path | str, base_url: str, secret: bytes):
        self.root, self.base_url, self._secret = Path(root), base_url.rstrip("/"), secret
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / checked_key(key)

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.is_file():
            raise NotFound(f"no blob {key}")
        return p.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, prefix: str) -> None:
        p = self._path(prefix)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.is_file():
            p.unlink()

    def list(self, prefix: str) -> list[str]:
        p = self._path(prefix)
        if not p.is_dir():
            return []
        return sorted(f.relative_to(self.root).as_posix() for f in p.rglob("*") if f.is_file() and not f.name.startswith("."))

    # signed URLs, served by the API's blob route
    def sign(self, method: str, key: str, ttl_s: int) -> str:
        expires = int(time.time()) + ttl_s
        claim = json.dumps({"m": method, "k": checked_key(key), "e": expires}, separators=(",", ":")).encode()
        mac = hmac.new(self._secret, claim, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(claim + mac).decode().rstrip("=")

    def verify(self, token: str, method: str) -> str:
        """The key a token grants for `method`, or Invalid."""
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            claim, mac = raw[:-32], raw[-32:]
            if not hmac.compare_digest(hmac.new(self._secret, claim, hashlib.sha256).digest(), mac):
                raise ValueError
            c = json.loads(claim)
            if c["m"] != method or c["e"] < time.time():
                raise ValueError
            return checked_key(c["k"])
        except (ValueError, KeyError, TypeError):
            raise Invalid("the blob URL is not valid or has expired") from None

    def presign_get(self, key: str, ttl_s: int = 900) -> str:
        return f"{self.base_url}/internal/blobs/{self.sign('GET', key, ttl_s)}"

    def presign_put(self, key: str, ttl_s: int = 900) -> str:
        return f"{self.base_url}/internal/blobs/{self.sign('PUT', key, ttl_s)}"
