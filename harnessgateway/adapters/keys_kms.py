"""KeyWrapper with the master key in AWS KMS (`HG_KMS_KEY` = a key id, ARN or alias/name): Encrypt and Decrypt, bound
to the record by the encryption context. The process needs kms:Encrypt and kms:Decrypt on that key, nothing else."""
from __future__ import annotations

import boto3


class KmsKeys:
    def __init__(self, key_id: str):
        self._key_id = key_id
        self._kms = boto3.client("kms")

    def wrap(self, key: bytes, context: str) -> bytes:
        out = self._kms.encrypt(KeyId=self._key_id, Plaintext=key, EncryptionContext={"record": context})
        return bytes(out["CiphertextBlob"])

    def unwrap(self, wrapped: bytes, context: str) -> bytes:
        return bytes(self._kms.decrypt(CiphertextBlob=wrapped, EncryptionContext={"record": context})["Plaintext"])
