"""The access table (SPEC §9.2): what each principal may do, and how a refusal reads."""
import pytest

from harnessgateway.domain.access import TABLE, Principal
from harnessgateway.errors import Forbidden

ADMIN = Principal("org_" + "0" * 32, "account:a", "admin")
DEV = Principal("org_" + "0" * 32, "account:d", "developer")
APP = Principal("org_" + "0" * 32, "key:k", "developer", run_only=True)


def test_admins_may_everything():
    for resource, actions in TABLE.items():
        for action in actions:
            ADMIN.require(resource, action)


def test_developers_build_and_read_the_organization():
    for resource, action in (("harnesses", "install"), ("keys", "create"), ("responses", "create"), ("providers", "read"),
                             ("egress", "read"), ("usage", "read")):
        DEV.require(resource, action)
    for resource, action in (("providers", "create"), ("egress", "update"), ("accounts", "create"), ("audit", "read")):
        with pytest.raises(Forbidden):
            DEV.require(resource, action)


def test_a_run_only_key_runs_and_reads_its_own():
    for resource, action in (("responses", "create"), ("responses", "cancel"), ("files", "create"), ("harnesses", "read"),
                             ("providers", "read")):
        APP.require(resource, action)
    for resource, action in (("harnesses", "install"), ("keys", "create"), ("egress", "read"), ("accounts", "read")):
        with pytest.raises(Forbidden):
            APP.require(resource, action)
    assert APP.owns_only and not DEV.owns_only


def test_a_refusal_names_who_may():
    with pytest.raises(Forbidden, match="an admin may update egress"):
        DEV.require("egress", "update")
    with pytest.raises(Forbidden, match="an admin or a developer may install harnesses"):
        APP.require("harnesses", "install")
