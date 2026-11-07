"""Configuration (SPEC §17): environment variables only, all `HG_` prefixed, read once and validated with every problem
reported at once. A role names what it needs; a process that lacks it refuses to start."""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .domain.limits import OrgLimits

ROLES = ("api", "worker", "door")


class ConfigError(SystemExit):
    def __init__(self, problems: list[str]):
        super().__init__("configuration error:\n  " + "\n  ".join(problems))


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    redis_url: str = "redis://localhost:6379/0"
    blob_url: str = ""
    secret_key: bytes = b""
    kms_key: str = ""
    operator_key: str = ""
    door_key: str = ""
    api_url: str = "http://localhost:8000"
    door: str = "hg-door:3128"
    host: str = "0.0.0.0"
    port: int = 8000
    sandbox_image: str = f"ghcr.io/harnessgateway/sandbox:{__version__}"
    platform_image: str = f"ghcr.io/harnessgateway/harnessgateway:{__version__}"
    sandbox_runtime: str = "runsc"
    door_container: str = "hg-door"
    box_dir: Path = Path("/var/lib/harnessgateway/boxes")
    upstream_proxy: str = ""
    ca_file: str = ""
    allow_private_egress: bool = False
    console_url: str = "http://localhost:8800"
    smtp_url: str = ""
    mail_from: str = ""
    builder_dir: Path = Path(__file__).resolve().parent.parent / "packages" / "harness-package-builder"
    extensions: tuple[str, ...] = ()
    log_level: str = "info"
    defaults: OrgLimits = field(default_factory=OrgLimits)

    @property
    def pepper(self) -> bytes:
        from .crypto import derive
        return derive(self.secret_key, "harnessgateway/pepper")

    @property
    def platform_hosts(self) -> list[str]:
        """The hosts a box may always reach through the door: the api, as boxes reach it (egress entry form)."""
        u = urlsplit(self.api_url)
        assert u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
        return [f"{u.hostname.lower()}:{port}"]

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
        try:
            defaults = OrgLimits.from_env({k[len("HG_DEFAULT_"):].lower(): v for k, v in e.items()
                                           if k.startswith("HG_DEFAULT_")})
        except ValueError as err:
            problems.append(str(err))
            defaults = OrgLimits()
        s = cls(database_url=get("DATABASE_URL"), redis_url=get("REDIS_URL", cls.redis_url), blob_url=get("BLOB_URL"),
                secret_key=secret, kms_key=get("KMS_KEY"), operator_key=get("OPERATOR_KEY"), door_key=get("DOOR_KEY"),
                api_url=get("API_URL", cls.api_url).rstrip("/"), door=get("DOOR", cls.door),
                host=get("HOST", cls.host), port=number("PORT", cls.port, 1, 65535),
                sandbox_image=get("SANDBOX_IMAGE", cls.sandbox_image), platform_image=get("PLATFORM_IMAGE", cls.platform_image),
                sandbox_runtime=get("SANDBOX_RUNTIME", cls.sandbox_runtime),
                door_container=get("DOOR_CONTAINER", cls.door_container), box_dir=Path(get("BOX_DIR", str(cls.box_dir))),
                upstream_proxy=get("UPSTREAM_PROXY"), ca_file=get("CA_FILE"),
                allow_private_egress=_bool(get("ALLOW_PRIVATE_EGRESS")),
                console_url=get("CONSOLE_URL", cls.console_url).rstrip("/"), smtp_url=get("SMTP_URL"),
                mail_from=get("MAIL_FROM"), builder_dir=Path(get("BUILDER_DIR", str(cls.builder_dir))),
                extensions=tuple(x.strip() for x in get("EXTENSIONS").split(",") if x.strip()),
                log_level=get("LOG_LEVEL", "info").lower(), defaults=defaults)
        wanted = set(roles)
        if wanted & {"api", "worker"}:
            for name, value in (("DATABASE_URL", s.database_url), ("BLOB_URL", s.blob_url)):
                if not value:
                    problems.append(f"HG_{name} is required")
        if wanted and not raw:
            problems.append("HG_SECRET_KEY is required")
        if wanted & {"api", "door"} and not s.door_key.startswith("hgd_"):
            problems.append("HG_DOOR_KEY is required (hgd_..., from harnessgateway keygen): doors authenticate with it")
        if s.sandbox_runtime not in ("runsc", "runc"):
            problems.append("HG_SANDBOX_RUNTIME is runsc or runc")
        if s.ca_file and not Path(s.ca_file).is_file():
            problems.append(f"HG_CA_FILE names no file: {s.ca_file}")
        if s.smtp_url and not s.mail_from:
            problems.append("HG_MAIL_FROM (the sender address) is required with HG_SMTP_URL")
        if s.log_level not in ("debug", "info", "warning", "error"):
            problems.append("HG_LOG_LEVEL is debug, info, warning or error")
        if not urlsplit(s.api_url).hostname:
            problems.append("HG_API_URL is a URL")
        if problems:
            raise ConfigError(problems)
        return s

    def describe(self) -> dict[str, object]:
        """The settings without secrets, for a start-up log line and the config route."""
        hidden = {"secret_key", "operator_key", "door_key", "database_url", "redis_url", "smtp_url"}
        return {f.name: str(getattr(self, f.name)) for f in fields(self) if f.name not in hidden}
