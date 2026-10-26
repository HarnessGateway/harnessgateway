"""Settings, read from the environment once at process start and validated for the roles the process runs. A process
with a missing or malformed setting refuses to start; it never runs half-configured. SaaS, self-hosted and hybrid
deployments differ only in these values."""
from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")

ROLES = ("api", "gateway", "node")
_HERE = Path(__file__).resolve().parent


class ConfigError(SystemExit):
    def __init__(self, message: str):
        super().__init__(f"configuration error: {message}")


def _builtin_dir() -> Path:
    for candidate in (_HERE / "builtin", _HERE.parent / "packages"):
        if candidate.is_dir():
            return candidate
    return _HERE / "builtin"


def _hex32(name: str, value: str) -> bytes:
    try:
        raw = bytes.fromhex(value.strip())
    except ValueError:
        raise ConfigError(f"{name} is 64 hex digits") from None
    if len(raw) != 32:
        raise ConfigError(f"{name} is 64 hex digits (32 bytes)")
    return raw


@dataclass(frozen=True)
class Settings:
    roles: tuple[str, ...]
    # control plane
    database_url: str = ""
    secret_key: bytes = b""
    kms_key_id: str = ""
    operator_key: str = ""
    signup: str = "invite"
    host: str = "0.0.0.0"
    port: int = 8000
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    builtin_dir: Path = field(default_factory=_builtin_dir)
    default_packages: tuple[str, ...] = ()
    prices: dict[str, float] = field(default_factory=dict)
    markup: float = 3.0
    # gateway
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8081
    # node
    api_url: str = ""
    node_join_token: str = ""
    node_name: str = field(default_factory=socket.gethostname)
    node_dir: Path = Path("/var/lib/harnessgateway")
    node_slots: int = 4
    node_warm_max: int = 16
    sandbox_image: str = "harnessgateway/sandbox:1"
    platform_image: str = "harnessgateway/platform:1"
    runtime: str = "runsc"
    insecure_runtime: bool = False
    gateway_url: str = ""
    uplink_network: str = "bridge"
    upstream_proxy: str = ""
    ca_file: Path | None = None
    allow_private_egress: bool = False
    log_level: str = "info"

    @classmethod
    def from_env(cls, roles: tuple[str, ...], env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)
        unknown = [r for r in roles if r not in ROLES]
        if unknown or not roles:
            raise ConfigError(f"roles are {', '.join(ROLES)}; got {', '.join(roles) or 'none'}")

        def get(name: str, default: str = "") -> str:
            return e.get(name, default).strip()

        def integer(name: str, default: int) -> int:
            try:
                return int(get(name, str(default)))
            except ValueError:
                raise ConfigError(f"{name} is an integer") from None

        prices_raw = get("HG_PRICES")
        if not prices_raw and get("HG_PRICES_FILE"):
            prices_raw = Path(get("HG_PRICES_FILE")).read_text()
        try:
            prices = {k: float(v) for k, v in json.loads(prices_raw or "{}").items()}
        except (ValueError, AttributeError):
            raise ConfigError("HG_PRICES is a JSON object of meter name to USD per unit") from None
        s = cls(
            roles=tuple(roles),
            database_url=get("HG_DATABASE_URL"),
            secret_key=_hex32("HG_SECRET_KEY", get("HG_SECRET_KEY")) if get("HG_SECRET_KEY") else b"",
            kms_key_id=get("HG_KMS_KEY_ID"),
            operator_key=get("HG_OPERATOR_KEY"),
            signup=get("HG_SIGNUP", "invite"),
            host=get("HG_HOST", "0.0.0.0"), port=integer("HG_PORT", 8000),
            s3_endpoint=get("HG_S3_ENDPOINT"), s3_bucket=get("HG_S3_BUCKET"), s3_region=get("HG_S3_REGION", "us-east-1"),
            s3_access_key=get("HG_S3_ACCESS_KEY"), s3_secret_key=get("HG_S3_SECRET_KEY"),
            builtin_dir=Path(get("HG_BUILTIN_DIR")) if get("HG_BUILTIN_DIR") else _builtin_dir(),
            default_packages=tuple(p.strip() for p in get("HG_DEFAULT_PACKAGES").split(",") if p.strip()),
            prices=prices, markup=float(get("HG_MARKUP", "3") or 3),
            gateway_host=get("HG_GATEWAY_HOST", "0.0.0.0"), gateway_port=integer("HG_GATEWAY_PORT", 8081),
            api_url=get("HG_API_URL").rstrip("/"), node_join_token=get("HG_NODE_JOIN_TOKEN"),
            node_name=get("HG_NODE_NAME") or socket.gethostname(),
            node_dir=Path(get("HG_NODE_DIR", "/var/lib/harnessgateway")), node_slots=integer("HG_NODE_SLOTS", 4),
            node_warm_max=integer("HG_NODE_WARM_MAX", 4 * integer("HG_NODE_SLOTS", 4)),
            sandbox_image=get("HG_SANDBOX_IMAGE", "harnessgateway/sandbox:1"),
            platform_image=get("HG_PLATFORM_IMAGE", "harnessgateway/platform:1"),
            runtime=get("HG_RUNTIME", "runsc"), insecure_runtime=get("HG_INSECURE_RUNTIME") in ("1", "true", "yes"),
            gateway_url=get("HG_GATEWAY_URL").rstrip("/"), uplink_network=get("HG_UPLINK_NETWORK", "bridge"),
            upstream_proxy=get("HG_UPSTREAM_PROXY"),
            ca_file=Path(get("HG_CA_FILE")).absolute() if get("HG_CA_FILE") else None,
            allow_private_egress=get("HG_ALLOW_PRIVATE_EGRESS") in ("1", "true", "yes"),
            log_level=get("HG_LOG_LEVEL", "info"),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.signup not in ("invite", "closed"):
            raise ConfigError("HG_SIGNUP is invite or closed")
        if self.markup < 1:
            raise ConfigError("HG_MARKUP is at least 1")
        if any(p < 0 for p in self.prices.values()):
            raise ConfigError("HG_PRICES values are non-negative")
        if {"api", "gateway"} & set(self.roles):
            for name, value in (("HG_DATABASE_URL", self.database_url), ("HG_SECRET_KEY", self.secret_key)):
                if not value:
                    raise ConfigError(f"{name} is required for the api and gateway roles")
        if {"api", "gateway"} & set(self.roles) and not self.s3_bucket:
            raise ConfigError("HG_S3_BUCKET is required for the api and gateway roles")
        if self.s3_bucket and not BUCKET.fullmatch(self.s3_bucket):
            raise ConfigError(f"HG_S3_BUCKET {self.s3_bucket!r} is not an S3 bucket name: 3-63 lowercase letters, digits, "
                              "dots and hyphens, starting and ending with a letter or digit")
        if "node" in self.roles:
            if not self.api_url:
                raise ConfigError("HG_API_URL is required for the node role")
            if not self.gateway_url:
                raise ConfigError("HG_GATEWAY_URL is required for the node role: how sandboxes reach the gateway")
            if self.runtime != "runsc" and not self.insecure_runtime:
                raise ConfigError("HG_RUNTIME must be runsc (gVisor); any other runtime needs HG_INSECURE_RUNTIME=1 and "
                                  "is for development only")
            if self.node_slots < 1:
                raise ConfigError("HG_NODE_SLOTS is at least 1")
            if self.node_warm_max < self.node_slots:
                raise ConfigError("HG_NODE_WARM_MAX (session boxes kept warm) is at least HG_NODE_SLOTS")
            if self.ca_file is not None and not self.ca_file.is_file():
                raise ConfigError(f"HG_CA_FILE {self.ca_file} is not a file")
