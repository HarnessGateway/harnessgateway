"""What the egress proxy is told about one turn or one install (spec §10.4, §11.2)."""

from dataclasses import dataclass
from typing import Any

from harnessgateway.domain.hosts import EgressPolicy, HostEntry


@dataclass(frozen=True)
class BoundSecret:
    """A secret and the only hosts it may go to."""

    hosts: tuple[HostEntry, ...]
    value: str
    placeholder: str | None = None             # what the sandbox holds in its place
    header: str | None = None                  # a header to set on every request to those hosts
    prefix: str = ""                           # written before the value in that header

    def bound_to(self, host: str, port: int) -> bool:
        return any(entry.matches(host, port) for entry in self.hosts)


@dataclass(frozen=True)
class Policy:
    org_id: str
    subject_id: str                            # the turn, or the install
    environment: str
    egress: EgressPolicy
    secrets: tuple[BoundSecret, ...] = ()

    def secrets_for(self, host: str, port: int) -> list[BoundSecret]:
        return [secret for secret in self.secrets if secret.bound_to(host, port)]

    def to_json(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id, "subject_id": self.subject_id, "environment": self.environment,
            "mode": self.egress.mode,
            "allow": [str(entry) for entry in self.egress.allow],
            "deny": [str(entry) for entry in self.egress.deny],
            "secrets": [{"hosts": [str(entry) for entry in secret.hosts], "value": secret.value,
                         "placeholder": secret.placeholder, "header": secret.header, "prefix": secret.prefix}
                        for secret in self.secrets],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Policy":
        def entries(values: list[str]) -> tuple[HostEntry, ...]:
            return tuple(HostEntry.parse(value) for value in values)
        return cls(
            org_id=data["org_id"], subject_id=data["subject_id"], environment=data["environment"],
            egress=EgressPolicy(mode=data["mode"], allow=entries(data["allow"]), deny=entries(data["deny"])),
            secrets=tuple(BoundSecret(hosts=entries(secret["hosts"]), value=secret["value"],
                                      placeholder=secret["placeholder"], header=secret["header"],
                                      prefix=secret["prefix"]) for secret in data["secrets"]),
        )
