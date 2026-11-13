"""Secret values at rest: a map of name to sealed value, as it is kept in a jsonb column (spec §10.5)."""

import base64
from collections.abc import Mapping

from harnessgateway.domain.environment import VALUE_MAX
from harnessgateway.domain.sealing import Sealer
from harnessgateway.errors import Invalid


def check_value(value: object, what: str) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= VALUE_MAX:
        raise Invalid(f"{what} is a string of 1 to {VALUE_MAX} characters", param=what)
    return value


def seal(sealer: Sealer, value: str, *, org: str, kind: str, name: str) -> str:
    return base64.b64encode(sealer.seal(value, org=org, kind=kind, name=name)).decode()


def open_all(sealer: Sealer, sealed: Mapping[str, str], *, org: str, kind: str) -> dict[str, str]:
    """Every value of a sealed map, opened. `kind` names the definition the map belongs to."""
    return {name: sealer.open(base64.b64decode(value), org=org, kind=kind, name=name)
            for name, value in sealed.items()}
