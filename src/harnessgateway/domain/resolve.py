"""From a request's model line to what its turn will run with."""

from dataclasses import dataclass

from harnessgateway.domain.line import NONE, Line
from harnessgateway.domain.resources import Environment, Harness, Provider
from harnessgateway.errors import Invalid


@dataclass(frozen=True)
class Resolved:
    line: Line
    environment: Environment
    harness: Harness
    provider: Provider | None = None           # None: the harness calls no model
    api: str | None = None                     # the API the harness will speak to the provider

    @property
    def url(self) -> str:
        return self.provider.apis[self.api] if self.provider and self.api else ""    # type: ignore[index]

    @property
    def model(self) -> str:
        return self.line.model if self.provider else ""


def resolve(line: Line, environment: Environment | None, harness: Harness | None, secrets_set: frozenset[str],
            provider: Provider | None) -> Resolved:
    """What a line means, given the definitions it names (None where there is none of that name) and which of
    the harness's secrets have a value. A line that cannot run is refused here, before anything is queued."""
    def refuse(why: str) -> Invalid:
        return Invalid(why, param="model")
    if environment is None:
        raise refuse(f"there is no environment named {line.environment!r}")
    if harness is None:
        raise refuse(f"there is no harness named {line.harness!r}")
    if line.harness not in environment.harnesses:
        raise refuse(f"environment {line.environment!r} does not carry harness {line.harness!r}: "
                     "add it to the environment's harnesses")
    if missing := harness.missing(secrets_set):
        raise refuse(f"harness {line.harness!r} needs a value for {', '.join(missing)}")
    speaks = harness.manifest.apis
    if not speaks:
        if (line.provider, line.model) != (NONE, NONE):
            raise refuse(f"harness {line.harness!r} calls no model: name it "
                         f"{line.environment}/{line.harness}/{NONE}/{NONE}")
        return Resolved(line, environment, harness)
    if NONE in (line.provider, line.model):
        raise refuse(f"harness {line.harness!r} calls a model: name a provider that speaks "
                     f"{' or '.join(speaks)}, and a model")
    if provider is None:
        raise refuse(f"there is no provider named {line.provider!r}")
    api = next((api for api in speaks if api in provider.apis), None)
    if api is None:
        raise refuse(f"provider {line.provider!r} speaks {', '.join(provider.apis)}, but harness "
                     f"{line.harness!r} speaks {' or '.join(speaks)}")
    return Resolved(line, environment, harness, provider, api)
