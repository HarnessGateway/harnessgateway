"""A provider: a model endpoint a harness thinks with (spec §10.1). Its key is kept apart, sealed."""

from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, field_validator

from harnessgateway.domain.contract import ApiName
from harnessgateway.domain.definition import Definition
from harnessgateway.domain.hosts import HostEntry
from harnessgateway.domain.modelpath import MODEL_MAX, NONE
from harnessgateway.domain.names import check_name

MODELS_MAX = 100


def _base_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError(f"a base URL is http:// or https:// and a host: {value!r}")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError(f"a base URL carries no credentials, query or fragment: {value!r}")
    return value.rstrip("/")


def _name(value: str) -> str:
    check_name(value, "name")
    if value == NONE:
        raise ValueError(f"{NONE} is not a provider's name: in a model string it means no provider")
    return value


def _models(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(value)) != len(value):
        raise ValueError("a model is listed twice")
    return value


class Provider(Definition):
    """`models` are the models the organization offers through this provider: what its people are shown to pick
    from. A request may name any model the endpoint serves."""

    name: Annotated[str, AfterValidator(_name)]
    apis: Annotated[dict[ApiName, str], Field(min_length=1)]
    models: Annotated[tuple[Annotated[str, Field(min_length=1, max_length=MODEL_MAX)], ...],
                      Field(max_length=MODELS_MAX), AfterValidator(_models)] = ()

    @field_validator("apis")
    @classmethod
    def _urls(cls, value: dict[ApiName, str]) -> dict[ApiName, str]:
        return {api: _base_url(url) for api, url in value.items()}

    def host(self, api: ApiName) -> HostEntry:
        """Where requests to this API go: what the provider's key is bound to."""
        url = urlsplit(self.apis[api])
        assert url.hostname is not None
        return HostEntry(url.hostname, url.port)
