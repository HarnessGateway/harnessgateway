"""The HTTP application: the platform's parts built from its settings (`Service`), and the routes
mounted on one FastAPI app. The documentation site is served at `/docs` and the OpenAPI reference
at `/reference`. A refusal is 400, 401, 403, 404 or 409 with `{"detail": …}`; a malformed body 422."""
from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..backends.blobs import open_blobs
from ..backends.events import open_events
from ..backends.kms import open_sealer
from ..backends.records import open_records
from ..config import Settings
from ..contract.package import CONTRACT
from ..domain.accounts import Accounts
from ..domain.catalog import Catalog
from ..domain.harnesses import Harnesses
from ..domain.keys import Keys
from ..domain.members import Members
from ..domain.nodes import Nodes
from ..domain.orgs import Orgs
from ..domain.providers import Providers
from ..domain.sessions import Sessions
from ..errors import Conflict, Invalid, NotFound, TooMany
from ..kernel.vault import Sealer
from ..services.housekeeping import Housekeeping
from ..services.intake import Intake
from ..services.scheduler import Scheduler
from . import control, deployer, internal, responses, signin
from .auth import Principals

STATUS = {Invalid: 400, NotFound: 404, Conflict: 409, TooMany: 429}
DOCS = Path(__file__).resolve().parents[1] / "docs"            # the built documentation site (mkdocs build), when present
VERSION = version("harnessgateway")


class Service:
    """The platform's parts, built from its settings; shared by every role in a process."""

    def __init__(self, settings: Settings, vault: Optional[Sealer] = None):
        self.settings = settings
        self.records = open_records(settings.records_url)
        self.vault = vault or open_sealer(settings.kms, os.environ, self.records)
        self.blobs = open_blobs(settings.blobs_url, base_url=settings.public_url, secret=self.vault.derive("blobs"))
        self.events = open_events(self.records, settings.records_url)
        self.orgs = Orgs(self.records, self.blobs)
        self.members = Members(self.records, self.orgs)
        self.keys = Keys(self.records, self.orgs, self.members)
        self.providers = Providers(self.records, self.vault, self.orgs, settings.allow_private)
        self.harnesses = Harnesses(self.records, self.blobs, settings.data_dir / "git", self.orgs, settings.allow_private)
        self.nodes = Nodes(self.records)
        self.sessions = Sessions(self.records, self.blobs, self.events, self.orgs, self.harnesses, self.providers)
        self.intake = Intake(self.records, self.orgs, self.sessions)
        self.scheduler = Scheduler(self.sessions, self.harnesses, self.providers, self.orgs, self.nodes, settings.gateway_url)
        self.catalog = Catalog()
        self.accounts = Accounts(self.records, self.vault, self.orgs, self.members)
        self.housekeeping = Housekeeping(self.scheduler)
        self.join_token = settings.join_token or secrets.token_urlsafe(32)   # an in-process node joins with it
        self.principals = Principals(self.keys, self.nodes, settings.deployer_key, self.join_token, self.accounts)


def create_app(service: Service) -> FastAPI:
    settings = service.settings

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(service.housekeeping.run())
        yield
        task.cancel()

    app = FastAPI(title="HarnessGateway", version=VERSION, lifespan=lifespan, docs_url="/reference", redoc_url=None)

    for error, code in STATUS.items():
        @app.exception_handler(error)
        async def refused(_: Request, e: Exception, code: int = code) -> JSONResponse:
            return JSONResponse({"detail": str(e)}, status_code=code, headers={"Retry-After": "60"} if code == 429 else None)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": VERSION, "contract": CONTRACT, "roles": list(settings.roles), "signup": settings.signup}

    app.include_router(responses.router(service))
    app.include_router(control.router(service))
    app.include_router(signin.router(service, settings))
    app.include_router(deployer.router(service))
    app.include_router(internal.router(service, VERSION))

    if (DOCS / "index.html").is_file():
        app.mount("/docs", StaticFiles(directory=DOCS, html=True), name="docs")

        @app.get("/", include_in_schema=False)
        def home() -> RedirectResponse:
            return RedirectResponse("/docs/", status_code=302)

    return app
