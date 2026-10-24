"""The master key in AWS KMS. The platform asks KMS for one data key, keeps it only as the ciphertext
KMS returned (in the records, under the empty tenant), and decrypts it in memory when a process
starts; every seal and open is then local AES-GCM under that data key, so KMS sees one call per
process, not one per secret. Rotating the KMS key re-wraps the data key; rotating the data key is
`harnessgateway init --rewrap`, which is not needed for the KMS key's own rotation."""
from __future__ import annotations

import base64

from ...kernel.vault import Vault

RECORD = ("kms", "", "data-key")


class AwsKms:
    def __init__(self, key_id: str, records, region: str | None = None, endpoint_url: str | None = None, client=None):
        if not key_id:
            raise RuntimeError("set HG_KMS_KEY_ID to the KMS key the platform seals under")
        if records is None:
            raise RuntimeError("the AWS KMS backend keeps its wrapped data key in the records store")
        import boto3
        self.key_id, self.records = key_id, records
        self._kms = client or boto3.client("kms", region_name=region, endpoint_url=endpoint_url)
        kept = records.get(*RECORD)
        if kept and kept.get("key_id") == key_id:
            plain = self._kms.decrypt(CiphertextBlob=base64.b64decode(kept["wrapped"]), KeyId=key_id)["Plaintext"]
        else:
            made = self._kms.generate_data_key(KeyId=key_id, KeySpec="AES_256")
            plain = made["Plaintext"]
            records.put(*RECORD, {"key_id": key_id, "wrapped": base64.b64encode(made["CiphertextBlob"]).decode()})
        self._vault = Vault(plain)

    def seal(self, value: str, aad: str) -> str:
        return self._vault.seal(value, aad)

    def open(self, sealed: str, aad: str) -> str:
        return self._vault.open(sealed, aad)

    def derive(self, label: str) -> bytes:
        return self._vault.derive(label)
