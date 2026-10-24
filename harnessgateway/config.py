"""The platform's settings, from the environment, each with a default. Read when a process starts, not
when this module is imported. The same variables configure every role, and every backend is named
here: a managed deployment and a self-hosted one differ in these names and nothing else."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

ROLES = ("api", "gateway", "worker")


@dataclass(frozen=True)
class Settings:
    roles: tuple[str, ...] = ROLES                   # --role api,gateway,worker | all
    host: str = "127.0.0.1"                          # HG_HOST: the API's bind address
    port: int = 8000                                 # HG_PORT
    public_url: str = ""                             # HG_PUBLIC_URL: how workers and signed blob URLs reach the API
    data_dir: Path = field(default_factory=lambda: Path.home() / ".harnessgateway")   # HG_DATA_DIR
    records_url: str = ""                            # HG_RECORDS: sqlite:///… (default) or postgresql://…
    blobs_url: str = ""                              # HG_BLOBS: dir:///… (default) or s3://bucket
    signup: str = "closed"                           # HG_SIGNUP: open (a sign-up page creates orgs) or closed
    org: str = "main"                              # HG_ORG / HG_OWNER: the org and owner the first boot creates, sign-up closed
    owner: str = "owner"
    deployer_key: str = ""                           # HG_DEPLOYER_KEY: overrides the stored deployer key
    join_token: str = ""                             # HG_JOIN_TOKEN: what a worker presents to join
    gateway_host: str = "0.0.0.0"                    # HG_GATEWAY_HOST / HG_GATEWAY_PORT: where the gateway listens
    gateway_port: int = 8081
    gateway_url: str = ""                            # HG_GATEWAY_URL: how a run's door reaches the gateway
    base_image: str = ""                             # HG_BASE_IMAGE: the sandbox image; empty pulls the shipped one, or builds it
    runtime: str = ""                                # HG_RUNTIME: a sandboxed Docker runtime, e.g. runsc (gVisor)
    sandbox: str = "docker"                          # HG_SANDBOX: docker (the sandbox) or process (development only, no isolation)
    trace_sharing: bool = False                      # HG_TRACE_SHARING=1: free-plan organizations share traces unless they opt out
    allow_private: bool = False                      # HG_ALLOW_PRIVATE=1: egress may name private addresses (self-hosted)
    ca_file: Path | None = None                      # HG_CA_FILE: a CA bundle, where the network re-signs TLS
    worker_name: str = field(default_factory=socket.gethostname)   # HG_WORKER_NAME
    worker_slots: int = 4                            # HG_WORKER_SLOTS: turns at once on this node
    control_url: str = ""                            # HG_CONTROL_URL: the API, for a worker on another machine
    github_client_id: str = ""                       # HG_GITHUB_CLIENT_ID / _SECRET: sign in with GitHub, when set
    github_client_secret: str = ""
    google_client_id: str = ""                       # HG_GOOGLE_CLIENT_ID / _SECRET: sign in with Google, when set
    google_client_secret: str = ""
    smtp_url: str = ""                               # HG_SMTP_URL: smtp(s)://user:pass@host:port; set, sign-ups verify their email
    mail_from: str = ""                              # HG_MAIL_FROM: the sender

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).absolute())
        object.__setattr__(self, "public_url", (self.public_url or f"http://127.0.0.1:{self.port}").rstrip("/"))
        object.__setattr__(self, "records_url", self.records_url or f"sqlite:///{self.data_dir / 'records.db'}")
        object.__setattr__(self, "blobs_url", self.blobs_url or f"dir:///{self.data_dir / 'blobs'}")
        object.__setattr__(self, "gateway_url", (self.gateway_url or f"http://host.docker.internal:{self.gateway_port}").rstrip("/"))
        object.__setattr__(self, "control_url", (self.control_url or self.public_url).rstrip("/"))
        if self.ca_file is not None:
            object.__setattr__(self, "ca_file", Path(self.ca_file).absolute())
        if self.signup not in ("open", "closed"):
            raise ValueError("HG_SIGNUP is open or closed")
        if unknown := [r for r in self.roles if r not in ROLES]:
            raise ValueError(f"unknown roles {unknown}; roles are {', '.join(ROLES)} or all")
        if self.sandbox not in ("docker", "process"):
            raise ValueError("HG_SANDBOX is docker or process")

    @classmethod
    def from_env(cls, roles: str = "all") -> Settings:
        env = os.environ
        return cls(roles=ROLES if roles == "all" else tuple(r.strip() for r in roles.split(",") if r.strip()),
                   host=env.get("HG_HOST", cls.host), port=int(env.get("HG_PORT", cls.port)),
                   public_url=env.get("HG_PUBLIC_URL", ""), data_dir=Path(env.get("HG_DATA_DIR") or Path.home() / ".harnessgateway"),
                   records_url=env.get("HG_RECORDS", ""), blobs_url=env.get("HG_BLOBS", ""), signup=env.get("HG_SIGNUP", "closed"),
                   org=env.get("HG_ORG", cls.org), owner=env.get("HG_OWNER", cls.owner),
                   deployer_key=env.get("HG_DEPLOYER_KEY", ""), join_token=env.get("HG_JOIN_TOKEN", ""),
                   gateway_host=env.get("HG_GATEWAY_HOST", cls.gateway_host), gateway_port=int(env.get("HG_GATEWAY_PORT", cls.gateway_port)),
                   gateway_url=env.get("HG_GATEWAY_URL", ""), base_image=env.get("HG_BASE_IMAGE", ""), runtime=env.get("HG_RUNTIME", ""),
                   sandbox=env.get("HG_SANDBOX", cls.sandbox), trace_sharing=env.get("HG_TRACE_SHARING", "") in ("1", "true", "yes"),
                   allow_private=env.get("HG_ALLOW_PRIVATE", "") in ("1", "true", "yes"),
                   ca_file=Path(env["HG_CA_FILE"]) if env.get("HG_CA_FILE") else None,
                   worker_name=env.get("HG_WORKER_NAME") or socket.gethostname(), worker_slots=int(env.get("HG_WORKER_SLOTS", cls.worker_slots)),
                   control_url=env.get("HG_CONTROL_URL", ""),
                   github_client_id=env.get("HG_GITHUB_CLIENT_ID", ""), github_client_secret=env.get("HG_GITHUB_CLIENT_SECRET", ""),
                   google_client_id=env.get("HG_GOOGLE_CLIENT_ID", ""), google_client_secret=env.get("HG_GOOGLE_CLIENT_SECRET", ""),
                   smtp_url=env.get("HG_SMTP_URL", ""), mail_from=env.get("HG_MAIL_FROM", ""))
