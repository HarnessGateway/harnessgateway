"""Blobs in an S3-API bucket: AWS, R2, B2, MinIO, Garage."""
from __future__ import annotations

from ...errors import NotFound
from . import checked_key


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
