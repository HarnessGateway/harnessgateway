"""BlobStore on any S3-compatible service. Credentials come from the standard AWS chain, never from the URL.

    s3://bucket[/prefix][?region=eu-west-1][&endpoint=http://minio:9000]
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

CHUNK = 1024 * 1024


class S3Blobs:
    def __init__(self, url: str):
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if parts.scheme != "s3" or not parts.netloc:
            raise ValueError("an S3 blob URL is s3://bucket[/prefix][?endpoint=...&region=...]")
        self.bucket, self.prefix = parts.netloc, parts.path.strip("/") + "/" if parts.path.strip("/") else ""
        endpoint = query.get("endpoint", "")
        config = Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"},
                        s3={"addressing_style": "path"} if endpoint else {})
        kwargs: dict[str, Any] = {"region_name": query.get("region") or None, "config": config}
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        self._s3 = boto3.client("s3", **kwargs)
        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._s3.create_bucket(Bucket=self.bucket)

    def _k(self, key: str) -> str:
        return self.prefix + key

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._s3.put_object, Bucket=self.bucket, Key=self._k(key), Body=data)

    async def put_file(self, key: str, path: Path) -> None:
        await asyncio.to_thread(self._s3.upload_file, str(path), self.bucket, self._k(key))

    async def get(self, key: str) -> bytes:
        def run() -> bytes:
            try:
                return self._s3.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()  # type: ignore[no-any-return]
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    raise KeyError(key) from None
                raise
        return await asyncio.to_thread(run)

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        body = (await asyncio.to_thread(self._s3.get_object, Bucket=self.bucket, Key=self._k(key)))["Body"]
        try:
            while chunk := await asyncio.to_thread(body.read, CHUNK):
                yield chunk
        finally:
            body.close()

    async def size(self, key: str) -> int | None:
        try:
            head = await asyncio.to_thread(self._s3.head_object, Bucket=self.bucket, Key=self._k(key))
        except ClientError:
            return None
        return int(head["ContentLength"])

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._s3.delete_object, Bucket=self.bucket, Key=self._k(key))

    async def delete_prefix(self, prefix: str) -> None:
        def run() -> None:
            for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self._k(prefix)):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    self._s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True})
        await asyncio.to_thread(run)

    def link_get(self, key: str, ttl_s: int) -> str:
        return str(self._s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": self._k(key)},
                                                   ExpiresIn=ttl_s))

    def link_put(self, key: str, ttl_s: int) -> str:
        return str(self._s3.generate_presigned_url("put_object", Params={"Bucket": self.bucket, "Key": self._k(key)},
                                                   ExpiresIn=ttl_s))
