"""What every suite that needs a database shares: scratch databases on the Postgres HG_TEST_DATABASE_URL names (a
superuser URL), each owned by an ordinary role as a deploy connects (no superuser, no BYPASSRLS), dropped afterwards.
Without the variable, the suites that need one skip."""
from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

ROLE, ROLE_PASSWORD = "hg_test", "hg-test"


@pytest.fixture(scope="session")
def scratch_database() -> Iterator[Callable[[], str]]:
    """A function that creates a fresh database and returns the ordinary role's URL to it."""
    admin = os.environ.get("HG_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("needs HG_TEST_DATABASE_URL (a superuser URL of a scratch Postgres)")
    u = urlsplit(admin)
    made: list[str] = []

    def create() -> str:
        name = f"hg_test_{secrets.token_hex(4)}"
        with psycopg.connect(admin, autocommit=True) as cx:
            cx.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN "
                       f"CREATE ROLE {ROLE} LOGIN PASSWORD '{ROLE_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; "
                       "END IF; END $$")
            cx.execute(f"CREATE DATABASE {name} OWNER {ROLE}")
        made.append(name)
        return urlunsplit((u.scheme, f"{ROLE}:{ROLE_PASSWORD}@{u.hostname}:{u.port or 5432}", f"/{name}", "", ""))

    yield create
    with psycopg.connect(admin, autocommit=True) as cx:
        for name in made:
            cx.execute(f"DROP DATABASE {name} WITH (FORCE)")
