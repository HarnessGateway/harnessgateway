"""From a request's model string to what the turn will run with (spec §10.2)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from harnessgateway.domain.contract import ApiName
from harnessgateway.domain.environment import Environment
from harnessgateway.domain.harness import Harness
from harnessgateway.domain.modelpath import ModelPath
from harnessgateway.domain.names import DEFAULT_ENVIRONMENT
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
        """The model string written in full: what the response reports."""
        return ModelPath(
            harness=self.harness.name,
            environment=self.environment.name,
            provider=self.provider.name if self.provider else None,
            model=self.model,
        )


def _refuse(message: str) -> Invalid:
    return Invalid(message, param="model")


def _provider(path: ModelPath, harness: Harness, providers: Sequence[Provider]) -> Provider:
    wanted = path.provider or harness.provider
    if wanted:
        for provider in providers:
            if provider.name == wanted:
                return provider
        raise _refuse(f"there is no provider named {wanted!r}")
    for provider in providers:                                   # in the order they were created
        if any(api in provider.apis for api in harness.manifest.apis):
            return provider
    speaks = " or ".join(harness.manifest.apis)
    raise _refuse(f"harness {harness.name!r} needs a provider that speaks {speaks}: add one, or name one in the model")


def resolve(
    path: ModelPath,
    *,
    environments: Mapping[str, Environment],
    harnesses: Mapping[str, Harness],
    providers: Sequence[Provider],
) -> Resolved:
    environment = environments.get(path.environment or DEFAULT_ENVIRONMENT)
    if environment is None:
        raise _refuse(f"there is no environment named {path.environment!r}")
    harness = harnesses.get(path.harness)
    if harness is None:
        raise _refuse(f"there is no harness named {path.harness!r}")
    missing = harness.missing()
    if missing:
        raise _refuse(f"harness {harness.name!r} needs a value for {', '.join(missing)}")

    if not harness.manifest.apis:
        if path.provider:
            raise _refuse(f"harness {harness.name!r} uses no provider: the model is just {harness.name!r}")
        return Resolved(environment, harness, None, None, None)

    provider = _provider(path, harness, providers)
    api = next((api for api in harness.manifest.apis if api in provider.apis), None)
    if api is None:
        raise _refuse(
            f"provider {provider.name!r} speaks {', '.join(provider.apis)}, "
            f"but harness {harness.name!r} speaks {', '.join(harness.manifest.apis)}"
        )
    model = path.model or harness.model or provider.default_model
    if model is None:
        raise _refuse(
            f"no model: name one ({harness.name}/{provider.name}/<model>), "
            f"or set a default model on the harness or the provider"
        )
    return Resolved(environment, harness, provider, api, model)
