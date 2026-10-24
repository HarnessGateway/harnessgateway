"""Backend: where the master key lives. The `Sealer` protocol is the kernel's (`kernel.vault`); this
package holds the implementations: `local`, a master key in the environment or a file the platform
wrote, and `aws`, a key in AWS KMS that never leaves the service, with a data key the platform
seals under it."""
from __future__ import annotations

from typing import Mapping

from ...kernel.vault import Sealer


def open_sealer(kind: str, env: Mapping[str, str], records=None) -> Sealer:
    if kind == "local":
        from .local import open_local
        return open_local(env)
    if kind == "aws":
        from .aws import AwsKms
        return AwsKms(env.get("HG_KMS_KEY_ID", ""), records, region=env.get("AWS_REGION") or None, endpoint_url=env.get("HG_KMS_ENDPOINT") or None)
    raise ValueError(f"a kms backend is local or aws, not {kind!r}")
