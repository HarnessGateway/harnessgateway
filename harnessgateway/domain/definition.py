"""What every definition a user writes has in common: unknown fields are refused, and a refusal names the field."""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError

from harnessgateway.errors import Invalid

_PLAIN = {"extra_forbidden": "is not a known field", "missing": "is required"}


def describe(error: ValidationError) -> str:
    """The first thing wrong, as `field.path: what is wrong`."""
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"] if part != "[key]")
    what = _PLAIN.get(first["type"]) or first["msg"].removeprefix("Value error, ")
    return f"{where}: {what}" if where else what


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @classmethod
    def load(cls, data: object, **defaults: Any) -> Self:
        """The definition `data` describes, with `defaults` for fields it leaves out; or a refusal."""
        if not isinstance(data, dict):
            raise Invalid(f"expected an object, got {type(data).__name__}")
        try:
            return cls.model_validate({**defaults, **data})
        except ValidationError as e:
            raise Invalid(describe(e)) from None

    def dump(self) -> dict[str, Any]:
        """The definition as it is written: plain JSON that `load` reads back to the same thing."""
        return self.model_dump(mode="json")
