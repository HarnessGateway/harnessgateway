"""Layer 3: the files the platform keeps: package archives, workspace and home snapshots, traces and
captures. A directory on one box, an S3-API bucket for many. Workers never hold a credential for
either: they get a URL good for one object for a short while, a presigned S3 URL or, for the
directory, a signed URL the API serves itself.

Keys are slash-separated paths under a tenant prefix: `tenants/<tenant>/…`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import shutil
import time
from pathlib import Path
from typing import Protocol

from .errors import Invalid, NotFound

KEY = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")


def checked_key(key: str) -> str:
    if not isinstance(key, str) or not KEY.fullmatch(key) or ".." in key.split("/"):
        raise Invalid(f"a blob key is a slash-separated path of plain names: {key!r}")
    return key


class Blobs(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, prefix: str) -> None: ...
    def list(self, prefix: str) -> list[str]: ...
    def presign_get(self, key: str, ttl_s: int = 900) -> str: ...
    def presign_put(self, key: str, ttl_s: int = 900) -> str: ...


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


class S3Blobs:
    """Blobs in an S3-API bucket: AWS, R2, B2, MinIO, Garage. The endpoint, bucket and credentials come
    from the URL and the usual AWS variables."""

    def __init__(self, bucket: str, endpoint_url: str | None = None, region: str | None = None):
        import boto3
        from botocore.config import Config
        self.bucket = bucket
        self._s3 = boto3.client("s3", endpoint_url=endpoint_url, region_name=region,
                                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))

    def put(self, key: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=checked_key(key), Body=data)

    def get(self, key: str) -> bytes:
        try:
            return self._s3.get_object(Bucket=self.bucket, Key=checked_key(key))["Body"].read()
        except self._s3.exceptions.NoSuchKey:
            raise NotFound(f"no blob {key}") from None

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=checked_key(key))
            return True
        except self._s3.exceptions.ClientError:
            return False

    def delete(self, prefix: str) -> None:
        keys = self.list(prefix)
        for i in range(0, len(keys), 1000):
            self._s3.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})

    def list(self, prefix: str) -> list[str]:
        checked_key(prefix)
        keys: list[str] = []
        for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix.rstrip("/") + "/"):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return sorted(keys)

    def presign_get(self, key: str, ttl_s: int = 900) -> str:
        return self._s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": checked_key(key)}, ExpiresIn=ttl_s)

    def presign_put(self, key: str, ttl_s: int = 900) -> str:
        return self._s3.generate_presigned_url("put_object", Params={"Bucket": self.bucket, "Key": checked_key(key)}, ExpiresIn=ttl_s)


def open_blobs(url: str, *, base_url: str = "", secret: bytes = b"") -> Blobs:
    """`dir:///path` (presigned URLs served at `base_url`, signed with `secret`) or
    `s3://bucket` with `HG_S3_ENDPOINT` and AWS credentials in the environment."""
    if url.startswith("dir:///"):
        return DirBlobs(url[len("dir:///"):], base_url, secret)
    if url.startswith("s3://"):
        import os
        return S3Blobs(url[len("s3://"):].strip("/"), os.environ.get("HG_S3_ENDPOINT") or None, os.environ.get("AWS_REGION") or None)
    raise ValueError(f"blobs are dir:///path or s3://bucket, not {url!r}")
