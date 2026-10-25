"""Object storage: any S3-compatible service (AWS S3 for SaaS; RustFS or another S3 server self-hosted).
Every object an organization keeps is recorded in the `objects` ledger with its size, which is what storage metering
sums. Keys are always `orgs/<org>/...` for tenant data and `packages/<digest>.tgz` for shared package archives."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import IO, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .db import Database

CHUNK = 1024 * 1024


class Blobs:
    def __init__(self, db: Database, *, bucket: str, endpoint: str = "", region: str = "us-east-1",
                 access_key: str = "", secret_key: str = ""):
        self.db, self.bucket = db, bucket
        kwargs: dict[str, Any] = {"region_name": region,
                                  "config": Config(signature_version="s3v4", retries={"max_attempts": 5},
                                                   s3={"addressing_style": "path"} if endpoint else {})}
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        if access_key:
            kwargs.update(aws_access_key_id=access_key, aws_secret_access_key=secret_key)
        self._s3 = boto3.client("s3", **kwargs)

    async def ensure_bucket(self) -> None:
        def run() -> None:
            try:
                self._s3.head_bucket(Bucket=self.bucket)
            except ClientError:
                self._s3.create_bucket(Bucket=self.bucket)
        await asyncio.to_thread(run)

    async def put(self, key: str, data: bytes | IO[bytes], *, org_id: str | None, kind: str) -> int:
        """Store an object and record it; returns its size."""
        def run() -> int:
            if isinstance(data, bytes):
                self._s3.put_object(Bucket=self.bucket, Key=key, Body=data)
                return len(data)
            self._s3.upload_fileobj(data, self.bucket, key)
            return int(self._s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"])
        size = await asyncio.to_thread(run)
        async with self.db.platform() as tx:
            await tx.run("INSERT INTO objects (key, org_id, kind, bytes) VALUES (%s, %s, %s, %s) "
                         "ON CONFLICT (key) DO UPDATE SET bytes = EXCLUDED.bytes, deleted_at = NULL", key, org_id, kind, size)
        return size

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(lambda: self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())

    def stream(self, key: str, start: int | None = None, length: int | None = None) -> Iterator[bytes]:
        """A synchronous iterator over an object's bytes (Starlette runs it in a worker thread)."""
        extra = {"Range": f"bytes={start}-{start + length - 1}"} if start is not None and length else {}
        body = self._s3.get_object(Bucket=self.bucket, Key=key, **extra)["Body"]
        try:
            yield from body.iter_chunks(CHUNK)
        finally:
            body.close()

    async def exists(self, key: str) -> bool:
        def run() -> bool:
            try:
                self._s3.head_object(Bucket=self.bucket, Key=key)
                return True
            except ClientError:
                return False
        return await asyncio.to_thread(run)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(lambda: self._s3.delete_object(Bucket=self.bucket, Key=key))
        async with self.db.platform() as tx:
            await tx.run("UPDATE objects SET deleted_at = now() WHERE key = %s AND deleted_at IS NULL", key)

    async def delete_prefix(self, prefix: str) -> None:
        assert prefix.startswith("orgs/") and prefix.count("/") >= 2, "only an organization's own subtree is deleted"

        def run() -> None:
            pages = self._s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
            for page in pages:
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    self._s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True})
        await asyncio.to_thread(run)
        async with self.db.platform() as tx:
            await tx.run("UPDATE objects SET deleted_at = now() WHERE key LIKE %s AND deleted_at IS NULL",
                         prefix.replace("%", r"\%").replace("_", r"\_") + "%")

    async def probe(self) -> None:
        await asyncio.to_thread(lambda: self._s3.head_bucket(Bucket=self.bucket))


def org_key(org_id: str, *parts: str) -> str:
    assert org_id.startswith("org_") and all(p and "/" not in p and p not in (".", "..") for p in parts[:-1])
    return "/".join(("orgs", org_id, *parts))
