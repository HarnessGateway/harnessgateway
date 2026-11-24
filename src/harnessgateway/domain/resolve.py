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
    provider: Provider | None = None           # None: the line names no model

    @property
    def model(self) -> str:
        return self.line.model if self.provider else ""


def resolve(line: Line, environment: Environment | None, harness: Harness | None, provider: Provider | None,
            ) -> Resolved:
    """What a line means, given the definitions it names (None where there is none of that name). A line that
    names what is not there is refused here, before anything is queued. Whether a harness can use the provider
    it is given is the harness's to say, when it runs."""
    def refuse(why: str) -> Invalid:
        return Invalid(why, param="model")
    if environment is None:
        raise refuse(f"there is no environment named {line.environment!r}")
    if harness is None:
        raise refuse(f"there is no harness named {line.harness!r}")
    if line.harness not in environment.harnesses:
        raise refuse(f"environment {line.environment!r} does not carry harness {line.harness!r}: "
                     "add it to the environment's harnesses")
    if (line.provider, line.model) == (NONE, NONE):
        return Resolved(line, environment, harness)
    if NONE in (line.provider, line.model):
        raise refuse(f"name a provider and a model, or {NONE}/{NONE} for a harness that calls no model")
    if provider is None:
        raise refuse(f"there is no provider named {line.provider!r}")
    return Resolved(line, environment, harness, provider)
