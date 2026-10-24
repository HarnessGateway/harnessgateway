"""Backend: the files the platform keeps: package archives, workspace and home snapshots, traces and
captures. A directory on one box, an S3-API bucket for many. Workers never hold a credential for
either: they get a URL good for one object for a short while, a presigned S3 URL or, for the
directory, a signed URL the API serves itself.

Keys are slash-separated paths under a tenant prefix: `tenants/<tenant>/…`.
"""
from __future__ import annotations

import re
from typing import Protocol

from ...errors import Invalid

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


def open_blobs(url: str, *, base_url: str = "", secret: bytes = b"") -> Blobs:
    """`dir:///path` (presigned URLs served at `base_url`, signed with `secret`) or
    `s3://bucket` with `HG_S3_ENDPOINT` and AWS credentials in the environment."""
    if url.startswith("dir:///"):
        from .dir import DirBlobs
        return DirBlobs(url[len("dir:///"):], base_url, secret)
    if url.startswith("s3://"):
        import os
        from .s3 import S3Blobs
        return S3Blobs(url[len("s3://"):].strip("/"), os.environ.get("HG_S3_ENDPOINT") or None, os.environ.get("AWS_REGION") or None)
    raise ValueError(f"blobs are dir:///path or s3://bucket, not {url!r}")
