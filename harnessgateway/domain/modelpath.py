"""The `model` string of a request: environment/harness/provider/model, all four, always (spec §13.2)."""

from dataclasses import dataclass

from harnessgateway.domain.names import check_name
from harnessgateway.errors import Invalid

GRAMMAR = "environment/harness/provider/model"
NONE = "none"                                  # the provider and the model of a harness that calls no model
MODEL_MAX = 200


@dataclass(frozen=True)
class ModelPath:
    environment: str
    harness: str
    provider: str
    model: str

    @classmethod
    def parse(cls, value: object) -> "ModelPath":
        try:
            return cls._parse(value)
        except Invalid as e:
            raise Invalid(f"model is {GRAMMAR}, every part named (none/none for a harness that calls no "
                          f"model): {e.message}", param="model") from None

    @classmethod
    def _parse(cls, value: object) -> "ModelPath":
        if not isinstance(value, str):
            raise Invalid(f"expected a string, got {type(value).__name__}")
        parts = value.split("/", 3)                       # the model is the rest, and may hold slashes
        if len(parts) != 4 or not all(parts):
            raise Invalid(f"four parts are needed in {value!r}")
        environment, harness, provider, model = parts
        if len(model) > MODEL_MAX:
            raise Invalid(f"the model part is at most {MODEL_MAX} characters")
        return cls(check_name(environment, "environment"), check_name(harness, "harness"),
                   check_name(provider, "provider"), model)

    def __str__(self) -> str:
        return f"{self.environment}/{self.harness}/{self.provider}/{self.model}"
