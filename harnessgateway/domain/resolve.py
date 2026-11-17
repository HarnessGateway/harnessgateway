"""From a request's model string to what the turn will run with (spec §10.2)."""

from collections.abc import Mapping
from dataclasses import dataclass

from harnessgateway.domain.contract import ApiName
from harnessgateway.domain.environment import Environment
from harnessgateway.domain.harness import Harness
from harnessgateway.domain.modelpath import NONE, ModelPath
from harnessgateway.domain.provider import Provider
from harnessgateway.errors import Invalid


@dataclass(frozen=True)
class Resolved:
    environment: Environment
    harness: Harness
    provider: Provider | None
    api: ApiName | None
    model: str | None

    @property
    def url(self) -> str | None:
        return self.provider.apis[self.api] if self.provider and self.api else None

    @property
    def path(self) -> ModelPath:
        """The model string it was resolved from."""
        return ModelPath(self.environment.name, self.harness.name, self.provider.name if self.provider else NONE,
                         self.model or NONE)


def _refuse(message: str) -> Invalid:
    return Invalid(message, param="model")


def resolve(
    path: ModelPath,
    *,
    environments: Mapping[str, Environment],
    harnesses: Mapping[str, Harness],
    providers: Mapping[str, Provider],
) -> Resolved:
    environment = environments.get(path.environment)
    if environment is None:
        raise _refuse(f"there is no environment named {path.environment!r}")
    harness = harnesses.get(path.harness)
    if harness is None:
        raise _refuse(f"there is no harness named {path.harness!r}")
    if harness.name not in environment.harnesses:
        raise _refuse(f"environment {environment.name!r} does not carry harness {harness.name!r}: "
                      f"add it to the environment's harnesses")
    missing = harness.missing()
    if missing:
        raise _refuse(f"harness {harness.name!r} needs a value for {', '.join(missing)}")

    if not harness.manifest.apis:
        if (path.provider, path.model) != (NONE, NONE):
            raise _refuse(f"harness {harness.name!r} calls no model: name it "
                          f"{environment.name}/{harness.name}/{NONE}/{NONE}")
        return Resolved(environment, harness, None, None, None)

    speaks = " or ".join(harness.manifest.apis)
    if path.provider == NONE:
        raise _refuse(f"harness {harness.name!r} calls a model: name a provider that speaks {speaks}")
    provider = providers.get(path.provider)
    if provider is None:
        raise _refuse(f"there is no provider named {path.provider!r}")
    api = next((api for api in harness.manifest.apis if api in provider.apis), None)
    if api is None:
        raise _refuse(f"provider {provider.name!r} speaks {', '.join(provider.apis)}, "
                      f"but harness {harness.name!r} speaks {speaks}")
    if path.model == NONE:
        raise _refuse(f"harness {harness.name!r} calls a model: name it")
    return Resolved(environment, harness, provider, api, path.model)
