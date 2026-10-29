"""Configuration: environment variables only, all `HG_` prefixed (SPEC §10.6), read once and validated with every
problem reported at once."""
from __future__ import annotations

import base64
import os
import socket
from dataclasses import dataclass, field, fields
from pathlib import Path

from . import __version__
from .domain.capacity import Capacity

ROLES = ("api", "gateway", "node")
_PACKAGES = Path(__file__).resolve().parent.parent / "packages"


class ConfigError(SystemExit):
    def __init__(self, problems: list[str]):
        super().__init__("configuration error:\n  " + "\n  ".join(problems))


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    blob_url: str = ""
    secret_key: bytes = b""
    operator_key: str = ""
    kms_key: str = ""
    api_url: str = "http://localhost:8000"
    gateway_url: str = "http://localhost:8000/gateway"
    host: str = "0.0.0.0"
    port: int = 8000
    node_join_token: str = ""
    node_name: str = field(default_factory=socket.gethostname)
    node_pool: str = ""
    node_slots: int = 4
    node_warm_max: int = 32
    node_dir: Path = Path("/var/lib/harnessgateway")
    sandbox_image: str = f"ghcr.io/harnessgateway/sandbox:{__version__}"
    platform_image: str = f"ghcr.io/harnessgateway/harnessgateway:{__version__}"
    uplink_network: str = "hg-uplink"
    runtime: str = "runsc"
    upstream_proxy: str = ""
    ca_file: str = ""
    allow_private_egress: bool = False
    extensions: tuple[str, ...] = ()
    packages_dir: Path = _PACKAGES
    log_level: str = "info"
    defaults: Capacity = field(default_factory=Capacity)

    @property
    def pepper(self) -> bytes:
        from .crypto import derive
        return derive(self.secret_key, "harnessgateway/pepper")

    @classmethod
    def load(cls, roles: tuple[str, ...] = (), env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)
        problems: list[str] = []

        def get(name: str, default: str = "") -> str:
            return e.get(f"HG_{name}", default).strip()

        def number(name: str, default: int, lo: int, hi: int) -> int:
            raw = get(name, str(default))
            try:
                value = int(raw)
            except ValueError:
                problems.append(f"HG_{name} is a whole number, not {raw!r}")
                return default
            if not lo <= value <= hi:
                problems.append(f"HG_{name} is {lo} to {hi}, not {value}")
            return value

        secret = b""
        if raw := get("SECRET_KEY"):
            try:
                secret = base64.b64decode(raw, validate=True)
            except ValueError:
                secret = b""
            if len(secret) < 32:
                problems.append("HG_SECRET_KEY is at least 32 random bytes, base64-encoded (harnessgateway keygen)")
        unsafe = get("UNSAFE_RUNTIME")
        try:
            defaults = Capacity.from_env({k[len("HG_DEFAULT_"):].lower(): v for k, v in e.items()
                                          if k.startswith("HG_DEFAULT_")})
        except ValueError as err:
            problems.append(str(err))
            defaults = Capacity()
        s = cls(database_url=get("DATABASE_URL"), blob_url=get("BLOB_URL"), secret_key=secret,
                operator_key=get("OPERATOR_KEY"), kms_key=get("KMS_KEY"),
                api_url=get("API_URL", cls.api_url).rstrip("/"), gateway_url=get("GATEWAY_URL", cls.gateway_url).rstrip("/"),
                host=get("HOST", cls.host), port=number("PORT", cls.port, 1, 65535),
                node_join_token=get("NODE_JOIN_TOKEN"), node_name=get("NODE_NAME") or socket.gethostname(),
                node_pool=get("NODE_POOL"), node_slots=number("NODE_SLOTS", cls.node_slots, 1, 512),
                node_warm_max=number("NODE_WARM_MAX", cls.node_warm_max, 1, 4096),
                node_dir=Path(get("NODE_DIR", str(cls.node_dir))),
                sandbox_image=get("SANDBOX_IMAGE", cls.sandbox_image), platform_image=get("PLATFORM_IMAGE", cls.platform_image),
                uplink_network=get("UPLINK_NETWORK", cls.uplink_network), runtime=unsafe or "runsc",
                upstream_proxy=get("UPSTREAM_PROXY"), ca_file=get("CA_FILE"),
                allow_private_egress=_bool(get("ALLOW_PRIVATE_EGRESS")),
                extensions=tuple(x.strip() for x in get("EXTENSIONS").split(",") if x.strip()),
                packages_dir=Path(get("PACKAGES_DIR", str(_PACKAGES))), log_level=get("LOG_LEVEL", "info").lower(),
                defaults=defaults)
        server = {"api", "gateway"} & set(roles)
        if server:
            for name, value in (("DATABASE_URL", s.database_url), ("SECRET_KEY", raw)):
                if not value:
                    problems.append(f"HG_{name} is required")
        if server and not s.blob_url:
            problems.append("HG_BLOB_URL is required: file:///path for one host, s3://bucket/prefix for many")
        if "node" in roles:
            if not s.node_join_token and not (s.node_dir / "node.json").is_file():
                problems.append("HG_NODE_JOIN_TOKEN is required for a node's first start")
            if s.ca_file and not Path(s.ca_file).is_file():
                problems.append(f"HG_CA_FILE names no file: {s.ca_file}")
        if s.log_level not in ("debug", "info", "warning", "error"):
            problems.append("HG_LOG_LEVEL is debug, info, warning or error")
        if problems:
            raise ConfigError(problems)
        return s

    def describe(self) -> dict[str, object]:
        """The settings without secrets, for a start-up log line."""
        hidden = {"secret_key", "operator_key", "node_join_token", "database_url"}
        return {f.name: str(getattr(self, f.name)) for f in fields(self) if f.name not in hidden}
