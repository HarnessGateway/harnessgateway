"""The model line: the one field of a Responses request that says where and with what it runs."""

from dataclasses import dataclass

from harnessgateway.errors import Invalid
from harnessgateway.ids import check_name

GRAMMAR = "environment/harness/provider/model"
NONE = "none"                                  # the provider and the model of a harness that calls no model
MODEL_MAX = 200


@dataclass(frozen=True)
class Line:
    environment: str
    harness: str
    provider: str
    model: str

    @classmethod
    def parse(cls, value: object) -> "Line":
        parts = value.split("/", 3) if isinstance(value, str) else []      # the model is the rest: it may hold slashes
        try:
            if len(parts) != 4 or not all(parts) or len(parts[3]) > MODEL_MAX:
                raise Invalid(f"four parts are needed, the last at most {MODEL_MAX} characters")
            return cls(check_name(parts[0], "environment"), check_name(parts[1], "harness"),
                       check_name(parts[2], "provider"), parts[3])
        except Invalid as refusal:
            raise Invalid(f"model is {GRAMMAR}, every part named ({NONE}/{NONE} for a harness that calls no "
                          f"model): {refusal.message}", param="model") from None

    def __str__(self) -> str:
        return f"{self.environment}/{self.harness}/{self.provider}/{self.model}"
