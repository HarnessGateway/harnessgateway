"""Who may do what (SPEC §9.2): the two roles, the run-only key, and the one table every service checks against.

A `Principal` is whoever a request runs as, resolved from a sign-in or an API key: its organization, its actor
(`account:<id>` or `key:<id>`), its role, whether it is a key that only runs, whether it holds the platform scope,
and the account behind it. `require(resource, action)` is the check; a refusal names the role that may."""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import Forbidden

ADMIN, DEVELOPER = "admin", "developer"
ROLES = (ADMIN, DEVELOPER)
RUN_ONLY = "run_only"                         # a developer key that only runs: what an application holds

# resource -> action -> the grants that allow it. `admin` is implied everywhere; `run_only` is the narrowest grant and
# is checked instead of `developer` for a key that only runs.
TABLE: dict[str, dict[str, frozenset[str]]] = {
    "organization": {"read": frozenset({DEVELOPER, RUN_ONLY})},
    "accounts": {"create": frozenset(), "read": frozenset(), "update": frozenset(), "reset": frozenset()},
    "providers": {"create": frozenset(), "read": frozenset({DEVELOPER, RUN_ONLY}), "update": frozenset(),
                  "delete": frozenset()},
    "egress": {"read": frozenset({DEVELOPER}), "update": frozenset()},
    "harnesses": {"install": frozenset({DEVELOPER}), "read": frozenset({DEVELOPER, RUN_ONLY}),
                  "configure": frozenset({DEVELOPER}), "uninstall": frozenset({DEVELOPER})},
    "keys": {"create": frozenset({DEVELOPER}), "read": frozenset({DEVELOPER}), "revoke": frozenset({DEVELOPER})},
    "responses": {"create": frozenset({DEVELOPER, RUN_ONLY}), "read": frozenset({DEVELOPER, RUN_ONLY}),
                  "cancel": frozenset({DEVELOPER, RUN_ONLY}), "delete": frozenset({DEVELOPER, RUN_ONLY})},
    "files": {"create": frozenset({DEVELOPER, RUN_ONLY}), "read": frozenset({DEVELOPER, RUN_ONLY}),
              "delete": frozenset({DEVELOPER, RUN_ONLY})},
    "usage": {"read": frozenset({DEVELOPER, RUN_ONLY})},
    "traces": {"read": frozenset({DEVELOPER, RUN_ONLY})},
    "audit": {"read": frozenset()},
}
WHO = {ADMIN: "an admin", DEVELOPER: "an admin or a developer", RUN_ONLY: "any key or account"}


@dataclass(frozen=True)
class Principal:
    org_id: str
    actor: str                        # account:<id> | key:<id> | what an extension names
    role: str                         # admin | developer
    run_only: bool = False            # a key that only runs
    platform_admin: bool = False      # the platform scope (SPEC §9.4)
    account_id: str | None = None     # the person: the sign-in's account, or the account that created the key
    name: str = ""                    # the account's name or the key's label, for display

    @property
    def grant(self) -> str:
        return RUN_ONLY if self.run_only else self.role

    def may(self, resource: str, action: str) -> bool:
        allowed = TABLE[resource][action]
        return self.role == ADMIN or self.grant in allowed

    def require(self, resource: str, action: str) -> None:
        """Forbidden, naming who may, unless this principal may `action` on `resource`."""
        if not self.may(resource, action):
            allowed = TABLE[resource][action]
            who = WHO[RUN_ONLY] if RUN_ONLY in allowed else WHO[DEVELOPER] if DEVELOPER in allowed else WHO[ADMIN]
            raise Forbidden(f"{who} may {action} {resource}")

    def require_platform(self) -> None:
        if not self.platform_admin:
            raise Forbidden("the platform's operator does this")

    @property
    def owns_only(self) -> bool:
        """Whether reads of sessions, files, usage and traces are confined to what this principal itself made."""
        return self.run_only
