"""Projects (SPEC §9.1): the unit a team builds in. A project holds harnesses, developers, keys and sessions; the
builder is installed into every new one; `resolve` turns a request's `HG-Project` (or its absence) into the project a
principal addresses."""
from __future__ import annotations

import builtins
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from .. import ids
from ..db import Database, Tx
from ..domain.access import ADMIN, Principal
from ..domain.names import NAME
from ..errors import Conflict, Forbidden, Invalid, NotFound
from .audit import Audit
from .harnesses import Harnesses
from .pages import ts

DEFAULT = "default"
log = logging.getLogger("harnessgateway.projects")


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "object": "project", "name": row["name"], "created_at": ts(row["created_at"]),
            "members": int(row.get("members") or 0), "harnesses": int(row.get("harnesses") or 0)}


COUNTS = ("(SELECT count(*) FROM project_members pm WHERE pm.project_id = p.id) AS members, "
          "(SELECT count(*) FROM harnesses h WHERE h.project_id = p.id) AS harnesses")


class Projects:
    def __init__(self, db: Database, harnesses: Harnesses, audit: Audit, builder_dir: Path):
        self.db, self.harnesses, self.audit, self.builder_dir = db, harnesses, audit, builder_dir

    async def create(self, org_id: str, actor: str, name: object) -> dict[str, Any]:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise Invalid("a project name is lowercase letters, digits, dots, dashes and underscores", param="name")
        async with self.db.tenant(org_id) as tx:
            if await tx.one("SELECT 1 FROM projects WHERE name = %s", name):
                raise Conflict(f"a project named {name} exists", param="name")
            pid = ids.new("project")
            await tx.run("INSERT INTO projects (id, org_id, name) VALUES (%s, %s, %s)", pid, org_id, name)
        if (self.builder_dir / "harness.json").is_file():
            await self.harnesses.install_folder(org_id, pid, self.builder_dir)
        else:
            log.warning("no harness package builder at %s (HG_BUILDER_DIR): project %s starts without it", self.builder_dir, name)
        await self.audit.write(org_id, actor, "project.create", pid, {"name": name})
        return await self.get(org_id, pid)

    async def list(self, org_id: str, account_id: str | None = None) -> list[dict[str, Any]]:
        """Every project of the organization, or the ones `account_id` belongs to."""
        async with self.db.tenant(org_id) as tx:
            if account_id is None:
                rows = await tx.all(f"SELECT p.*, {COUNTS} FROM projects p ORDER BY p.created_at")
            else:
                rows = await tx.all(f"SELECT p.*, {COUNTS} FROM projects p JOIN project_members pm ON pm.project_id = p.id "
                                    "WHERE pm.account_id = %s ORDER BY p.created_at", account_id)
        return [_view(r) for r in rows]

    async def _row(self, tx: Tx, project_id: str) -> dict[str, Any]:
        row = await tx.one(f"SELECT p.*, {COUNTS} FROM projects p WHERE p.id = %s", ids.check(project_id, "project"))
        if not row:
            raise NotFound(f"no project {project_id}")
        return row

    async def get(self, org_id: str, project_id: str) -> dict[str, Any]:
        async with self.db.tenant(org_id) as tx:
            return _view(await self._row(tx, project_id))

    async def update(self, org_id: str, actor: str, project_id: str, name: object) -> dict[str, Any]:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise Invalid("a project name is lowercase letters, digits, dots, dashes and underscores", param="name")
        async with self.db.tenant(org_id) as tx:
            await self._row(tx, project_id)
            if await tx.one("SELECT 1 FROM projects WHERE name = %s AND id <> %s", name, project_id):
                raise Conflict(f"a project named {name} exists", param="name")
            await tx.run("UPDATE projects SET name = %s WHERE id = %s", name, project_id)
        await self.audit.write(org_id, actor, "project.update", project_id, {"name": name})
        return await self.get(org_id, project_id)

    async def delete(self, org_id: str, actor: str, project_id: str) -> None:
        """Refused while the project has sessions: they are someone's work. Its harnesses, keys and members go with it,
        and so do the records of its deleted sessions and their turns."""
        async with self.db.tenant(org_id) as tx:
            await self._row(tx, project_id)
            if await tx.one("SELECT 1 FROM sessions WHERE project_id = %s AND status = 'active'", project_id):
                raise Conflict("the project has sessions; delete them first")
            if await tx.value("SELECT count(*) FROM projects") <= 1:
                raise Conflict("the organization keeps at least one project")
            await tx.run("DELETE FROM traces WHERE response_id IN (SELECT id FROM turns WHERE project_id = %s)", project_id)
            await tx.run("DELETE FROM idempotency WHERE turn_id IN (SELECT id FROM turns WHERE project_id = %s)", project_id)
            await tx.run("DELETE FROM projects WHERE id = %s", project_id)
        await self.audit.write(org_id, actor, "project.delete", project_id)

    # ── members ────────────────────────────────────────────────────────────────────────────────────────────────────
    async def members(self, org_id: str, project_id: str) -> builtins.list[dict[str, Any]]:
        async with self.db.tenant(org_id) as tx:
            await self._row(tx, project_id)
            rows = await tx.all("SELECT a.id, a.email, a.name, a.status FROM project_members pm "
                                "JOIN accounts a ON a.id = pm.account_id WHERE pm.project_id = %s ORDER BY a.email", project_id)
        return [{"id": r["id"], "object": "account", "email": r["email"], "name": r["name"], "status": r["status"]} for r in rows]

    async def add_member(self, org_id: str, actor: str, project_id: str, account_id: str) -> None:
        async with self.db.tenant(org_id) as tx:
            await self._row(tx, project_id)
            if not await tx.one("SELECT 1 FROM memberships WHERE account_id = %s AND org_id = %s",
                                ids.check(account_id, "account"), org_id):
                raise NotFound(f"no account {account_id} in this organization")
            await tx.run("INSERT INTO project_members (project_id, account_id, org_id) VALUES (%s, %s, %s) "
                         "ON CONFLICT DO NOTHING", project_id, account_id, org_id)
        await self.audit.write(org_id, actor, "project.member.add", project_id, {"account": account_id})

    async def remove_member(self, org_id: str, actor: str, project_id: str, account_id: str) -> None:
        async with self.db.tenant(org_id) as tx:
            if not await tx.run("DELETE FROM project_members WHERE project_id = %s AND account_id = %s",
                                ids.check(project_id, "project"), ids.check(account_id, "account")):
                raise NotFound(f"{account_id} is not a member of {project_id}")
        await self.audit.write(org_id, actor, "project.member.remove", project_id, {"account": account_id})

    async def is_member(self, org_id: str, project_id: str, account_id: str) -> bool:
        async with self.db.tenant(org_id) as tx:
            return bool(await tx.one("SELECT 1 FROM project_members WHERE project_id = %s AND account_id = %s",
                                     project_id, account_id))

    # ── which project a request addresses ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    async def _named(tx: Tx, header: str) -> str:
        row = await tx.one("SELECT id FROM projects WHERE id = %s OR name = %s", header, header)
        if not row:
            raise NotFound(f"no project {header}", param="HG-Project")
        return str(row["id"])

    async def for_key(self, p: Principal, header: str | None) -> Principal:
        """A key's request with an `HG-Project` header: an admin key addresses that project; a developer key's own
        project is the only one it may name."""
        if not header:
            return p
        async with self.db.tenant(p.org_id) as tx:
            project = await self._named(tx, header)
        if p.role != ADMIN and project != p.project:
            raise Forbidden("this key belongs to another project")
        return replace(p, project=project)

    async def resolve(self, org_id: str, role: str, account_id: str, header: str | None) -> str | None:
        """The project an account's request addresses (SPEC §9.3): the `HG-Project` header (an id or a name), which a
        developer must belong to; without it, a developer's only project, or none for an admin (organization-wide)."""
        async with self.db.tenant(org_id) as tx:
            if header:
                project = await self._named(tx, header)
                if role != ADMIN and not await tx.one("SELECT 1 FROM project_members WHERE project_id = %s AND account_id = %s",
                                                      project, account_id):
                    raise Forbidden(f"you are not a developer of project {header}")
                return project
            if role == ADMIN:
                return None
            rows = await tx.all("SELECT p.id, p.name FROM projects p JOIN project_members pm ON pm.project_id = p.id "
                                "WHERE pm.account_id = %s ORDER BY p.created_at", account_id)
        if not rows:
            raise Forbidden("you belong to no project yet; ask an admin to add you to one")
        if len(rows) > 1:
            raise Invalid(f"name the project with the HG-Project header: {', '.join(r['name'] for r in rows)}",
                          param="HG-Project")
        return str(rows[0]["id"])
