"""The `model` string of a request: [environment:]harness[/provider[/model]] (spec §13.2)."""

from dataclasses import dataclass

from harnessgateway.domain.names import check_name
from harnessgateway.errors import Invalid

GRAMMAR = "[environment:]harness[/provider[/model]]"
MODEL_MAX = 200


@dataclass(frozen=True)
class ModelPath:
    harness: str
    environment: str | None = None
    provider: str | None = None
    model: str | None = None

    @classmethod
    def parse(cls, value: object) -> "ModelPath":
        try:
            return cls._parse(value)
        except Invalid as e:
            raise Invalid(f"model is {GRAMMAR}: {e.message}", param="model") from None

    @classmethod
    def _parse(cls, value: object) -> "ModelPath":
        if not isinstance(value, str):
            raise Invalid(f"expected a string, got {type(value).__name__}")
        head, slash, rest = value.partition("/")
        environment, colon, harness = head.rpartition(":")
        provider, slash2, model = rest.partition("/")
        if (slash and not provider) or (slash2 and not model):
            raise Invalid(f"a part is empty in {value!r}")
        if len(model) > MODEL_MAX:
            raise Invalid(f"the model part is at most {MODEL_MAX} characters")
        return cls(
            harness=check_name(harness, "harness"),
            environment=check_name(environment, "environment") if colon else None,
            provider=check_name(provider, "provider") if slash else None,
            model=model or None,
        )

    def __str__(self) -> str:
        text = f"{self.environment}:{self.harness}" if self.environment else self.harness
        for part in (self.provider, self.model):
            if part is None:
                break
            text += f"/{part}"
        return text
