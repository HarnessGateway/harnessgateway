"""Who is calling: what every authenticated request resolves to (spec §16)."""

from dataclasses import dataclass

from harnessgateway.errors import Forbidden, Invalid

ROLES = ("runner", "developer", "admin")               # each may do what the ones before it may


def check_role(value: object) -> str:
    if value not in ROLES:
        raise Invalid(f"role is one of {', '.join(ROLES)}", param="role")
    return str(value)


@dataclass(frozen=True)
class Principal:
    org_id: str
    key_id: str | None
    role: str

    def require(self, role: str) -> None:
        """Refuse unless this caller's role is `role` or above."""
        if ROLES.index(self.role) < ROLES.index(role):
            raise Forbidden(f"this needs a key with the role {role}; this key's role is {self.role}")
