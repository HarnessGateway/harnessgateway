"""An installed harness: a package's manifest plus what the organization has set on it (spec §14.2)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from harnessgateway.domain.package import Manifest


@dataclass(frozen=True)
class Harness:
    manifest: Manifest
    digest: str
    provider: str | None = None                                  # the default provider, if one was chosen
    model: str | None = None                                     # the default model, if one was chosen
    settings_values: Mapping[str, str] = field(default_factory=dict)
    secrets_set: frozenset[str] = frozenset()                    # the names of the secrets that have a value
    added_at: datetime | None = None                             # when the organization first added it

    @property
    def name(self) -> str:
        return self.manifest.name

    def setting_values(self) -> dict[str, str]:
        """Each declared setting's value: what the organization set, else the package's default."""
        values = {}
        for name, setting in self.manifest.settings.items():
            value = self.settings_values.get(name, setting.default)
            if value is not None:
                values[name] = value
        return values

    def missing(self) -> list[str]:
        """Required settings and secrets that have no value."""
        have = self.setting_values()
        settings = [n for n, s in self.manifest.settings.items() if s.required and n not in have]
        secrets = [n for n, s in self.manifest.secrets.items() if s.required and n not in self.secrets_set]
        return settings + secrets
