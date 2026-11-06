"""Accounts and sign-ins (SPEC §10.1): people, their passwords, their one-time invitations and their bearer tokens.

An account is created invited and activated with a one-time token by setting a password; the same token resets one.
The invitation is a link to the console's setup page, mailed to the person when mail is configured and returned to
the caller either way, once.
Passwords are scrypt hashes; ten failed sign-ins lock an account for fifteen minutes; a sign-in is an opaque token kept
as an HMAC, expiring, revoked by logout, by a password change and by disabling the account. The identity tables are the
platform's, so this service runs on the platform scope and confines itself to the organization it is asked about."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from .. import ids
from ..domain.access import ROLES
from ..errors import Conflict, Invalid, Locked, NotFound, Unauthenticated
from ..ports.mail import Mailer
from ..store.db import Database, Tx
from .audit import Audit
from .pages import ts

log = logging.getLogger("harnessgateway.accounts")

PASSWORD_MIN = 12
LOCK_AFTER, LOCK_S = 10, 900
SIGN_IN_TTL_S = 12 * 3600
INVITATION_TTL_S = 7 * 86400
TOUCH_S = 60
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
WRONG = "unknown email or wrong password"


def _view(row: dict[str, Any], role: str | None = None) -> dict[str, Any]:
    out = {"id": row["id"], "object": "account", "email": row["email"], "name": row["name"], "status": row["status"],
           "platform_admin": row["platform_admin"], "created_at": ts(row["created_at"]), "signed_in_at": ts(row["signed_in_at"])}
    if role is not None:
        out["role"] = role
    return out


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        hash_password(password)                                 # the same cost whether or not the account exists
        return False
    _, salt, digest = stored.split("$")
    return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)).split("$")[2], digest)


def check_email(email: object) -> str:
    """The login address, normalized; Invalid otherwise. Checked before anything is written for the account."""
    if not isinstance(email, str) or not EMAIL.fullmatch(email.strip()):
        raise Invalid("email is the account's login address", param="email")
    return email.strip().lower()


def check_new_password(password: object) -> str:
    if not isinstance(password, str) or len(password) < PASSWORD_MIN:
        raise Invalid(f"a password is at least {PASSWORD_MIN} characters", param="password")
    return password


class Accounts:
    def __init__(self, db: Database, pepper: bytes, audit: Audit, console_url: str, mail: Mailer | None):
        self.db, self.pepper, self.audit, self.console_url, self.mail = db, pepper, audit, console_url, mail
        self._touched: dict[str, float] = {}

    async def _invitation(self, tx: Tx, account_id: str) -> str:
        """A fresh one-time token for the account (any earlier one is void), as the setup link it is used at."""
        token = ids.new_secret("invitation")
        await tx.run("DELETE FROM invitations WHERE account_id = %s", account_id)
        await tx.run("INSERT INTO invitations (digest, account_id, expires_at) VALUES (%s, %s, %s)",
                     ids.digest(token, self.pepper), account_id, datetime.now(UTC) + timedelta(seconds=INVITATION_TTL_S))
        return f"{self.console_url}/setup?token={quote(token)}"

    async def _deliver(self, email: str, link: str, org_name: str) -> bool:
        """The link by mail, when mail is configured. Never fails the request: the caller has the link too."""
        if self.mail is None:
            return False
        body = (f"You have been invited to HarnessGateway ({org_name}).\n\nChoose your password at this link; it works "
                f"once and expires in seven days:\n\n{link}\n")
        try:
            await asyncio.to_thread(self.mail.send, email, "Your HarnessGateway invitation", body)
            return True
        except Exception:
            log.exception("sending the invitation for %s failed", email)
            return False

    async def _audit(self, account_id: str, action: str, detail: dict[str, Any] | None = None) -> None:
        """An account's own event (a sign-in, a failure, a lock, an activation), in the log of every organization it
        belongs to: that is where its admins read it."""
        async with self.db.platform() as tx:
            orgs = [m["org_id"] for m in await tx.all("SELECT org_id FROM memberships WHERE account_id = %s", account_id)]
        for org_id in orgs or [None]:
            await self.audit.write(org_id, f"account:{account_id}", action, account_id, detail)

    # ── creating and inviting ──────────────────────────────────────────────────────────────────────────────────────
    async def create(self, org_id: str, actor: str, email: object, name: object, role: object,
                     platform_admin: bool = False) -> dict[str, Any]:
        """A new account in the organization, invited: the result carries its one-time invitation token, shown once.
        An account that exists elsewhere (another organization) joins with a membership and no new token."""
        email = check_email(email)
        if role not in ROLES:
            raise Invalid(f"role is one of {', '.join(ROLES)}", param="role")
        async with self.db.platform() as tx:
            row = await tx.one("SELECT * FROM accounts WHERE email = %s", email)
            if row and await tx.one("SELECT 1 FROM memberships WHERE account_id = %s AND org_id = %s", row["id"], org_id):
                raise Conflict(f"{email} is already a member of this organization", param="email")
            if not row:
                await tx.run("INSERT INTO accounts (id, email, name, platform_admin) VALUES (%s, %s, %s, %s)",
                             ids.new("account"), email, str(name or "")[:200], platform_admin)
                row = await tx.get("SELECT * FROM accounts WHERE email = %s", email)
            await tx.run("INSERT INTO memberships (account_id, org_id, role) VALUES (%s, %s, %s)", row["id"], org_id, role)
            link = await self._invitation(tx, row["id"]) if row["status"] == "invited" else None
            org = await tx.get("SELECT name FROM orgs WHERE id = %s", org_id)
        await self.audit.write(org_id, actor, "account.create", row["id"], {"email": email, "role": role})
        out = _view(row, str(role))
        if link:
            out |= {"invitation": link, "sent": await self._deliver(email, link, org["name"])}
        return out

    async def reset(self, org_id: str, actor: str, account_id: str) -> dict[str, Any]:
        """A new invitation: the person sets a new password with it; every sign-in of the account ends now."""
        async with self.db.platform() as tx:
            row = await self._member(tx, org_id, account_id)
            link = await self._invitation(tx, row["id"])
            await tx.run("UPDATE sign_ins SET revoked_at = now() WHERE account_id = %s AND revoked_at IS NULL", row["id"])
            org = await tx.get("SELECT name FROM orgs WHERE id = %s", org_id)
        await self.audit.write(org_id, actor, "account.reset", row["id"])
        return {**_view(row), "invitation": link, "sent": await self._deliver(row["email"], link, org["name"])}

    async def accept(self, token: object, password: object, client: str) -> dict[str, Any]:
        """Activate an invited account, or finish a reset: the token once, the new password; signed in on success."""
        if not isinstance(token, str) or not token.startswith(ids.SECRET["invitation"]):
            raise Invalid("token is the invitation", param="token")
        check_new_password(password)
        async with self.db.platform() as tx:
            inv = await tx.one("DELETE FROM invitations WHERE digest = %s RETURNING account_id, expires_at",
                               ids.digest(token, self.pepper))
            if not inv or inv["expires_at"] < datetime.now(UTC):
                raise Invalid("the invitation is unknown, used or expired; ask an admin for a new one", param="token")
            await tx.run("UPDATE accounts SET password_hash = %s, status = 'active', failures = 0, locked_until = NULL "
                         "WHERE id = %s AND status <> 'disabled'", hash_password(str(password)), inv["account_id"])
            await tx.run("UPDATE sign_ins SET revoked_at = now() WHERE account_id = %s AND revoked_at IS NULL", inv["account_id"])
            row = await tx.get("SELECT * FROM accounts WHERE id = %s", inv["account_id"])
            if row["status"] != "active":
                raise Unauthenticated("the account is disabled")
            signed = await self._sign_in(tx, row, client)
        await self._audit(row["id"], "account.activate")
        return signed

    # ── reading and changing ───────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    async def _member(tx: Tx, org_id: str, account_id: str) -> dict[str, Any]:
        row = await tx.one("SELECT a.*, m.role FROM accounts a JOIN memberships m ON m.account_id = a.id "
                           "WHERE a.id = %s AND m.org_id = %s", ids.check(account_id, "account"), org_id)
        if not row:
            raise NotFound(f"no account {account_id}")
        return row

    async def list(self, org_id: str) -> list[dict[str, Any]]:
        async with self.db.platform() as tx:
            rows = await tx.all("SELECT a.*, m.role FROM accounts a JOIN memberships m ON m.account_id = a.id "
                                "WHERE m.org_id = %s ORDER BY a.created_at", org_id)
        return [_view(r, r["role"]) for r in rows]

    async def get(self, org_id: str, account_id: str) -> dict[str, Any]:
        async with self.db.platform() as tx:
            row = await self._member(tx, org_id, account_id)
        return _view(row, row["role"])

    async def update(self, org_id: str, actor: str, account_id: str, *, role: object = None, status: object = None,
                     platform_admin: object = None, by_platform_admin: bool = False) -> dict[str, Any]:
        """Role, status (`active`, `disabled`) and, by a platform admin, the platform-admin flag. Disabling ends every
        sign-in and key of the account; the last admin of an organization cannot be demoted or disabled."""
        if role is not None and role not in ROLES:
            raise Invalid(f"role is one of {', '.join(ROLES)}", param="role")
        if status is not None and status not in ("active", "disabled"):
            raise Invalid("status is active or disabled", param="status")
        if platform_admin is not None and not by_platform_admin:
            raise Invalid("only a platform admin grants the platform scope", param="platform_admin")
        async with self.db.platform() as tx:
            row = await self._member(tx, org_id, account_id)
            if (role == "developer" or status == "disabled") and row["role"] == "admin" and await tx.value(
                    "SELECT count(*) FROM memberships m JOIN accounts a ON a.id = m.account_id WHERE m.org_id = %s "
                    "AND m.role = 'admin' AND a.status <> 'disabled'", org_id) <= 1:
                raise Conflict("the organization keeps at least one active admin")
            if role is not None:
                await tx.run("UPDATE memberships SET role = %s WHERE account_id = %s AND org_id = %s", role, row["id"], org_id)
            if status == "disabled":
                await tx.run("UPDATE accounts SET status = 'disabled' WHERE id = %s", row["id"])
                await tx.run("UPDATE sign_ins SET revoked_at = now() WHERE account_id = %s AND revoked_at IS NULL", row["id"])
                await tx.run("UPDATE api_keys SET revoked_at = now() WHERE account_id = %s AND revoked_at IS NULL", row["id"])
            elif status == "active":
                await tx.run("UPDATE accounts SET status = CASE WHEN password_hash IS NULL THEN 'invited' ELSE 'active' END, "
                             "failures = 0, locked_until = NULL WHERE id = %s", row["id"])
            if platform_admin is not None:
                await tx.run("UPDATE accounts SET platform_admin = %s WHERE id = %s", bool(platform_admin), row["id"])
            row = await self._member(tx, org_id, account_id)
        await self.audit.write(org_id, actor, "account.update", row["id"],
                               {k: v for k, v in (("role", role), ("status", status), ("platform_admin", platform_admin))
                                if v is not None})
        return _view(row, row["role"])

    async def change_password(self, account_id: str, current: object, new: object, keep_sign_in: str) -> None:
        """The account's own change: the current password, a new one; every other sign-in ends."""
        check_new_password(new)
        async with self.db.platform() as tx:
            row = await tx.get("SELECT * FROM accounts WHERE id = %s", account_id)
            if not check_password(str(current or ""), row["password_hash"]):
                raise Unauthenticated("the current password is wrong")
            await tx.run("UPDATE accounts SET password_hash = %s WHERE id = %s", hash_password(str(new)), account_id)
            await tx.run("UPDATE sign_ins SET revoked_at = now() WHERE account_id = %s AND revoked_at IS NULL AND digest <> %s",
                         account_id, ids.digest(keep_sign_in, self.pepper))
        await self._audit(account_id, "account.password")

    # ── signing in ─────────────────────────────────────────────────────────────────────────────────────────────────
    async def login(self, email: object, password: object, client: str) -> dict[str, Any]:
        """A sign-in for the right password of an active, unlocked account; every attempt is audited."""
        email = str(email or "").strip().lower()
        async with self.db.platform() as tx:
            row = await tx.one("SELECT * FROM accounts WHERE email = %s FOR UPDATE", email)
            if row is None:
                check_password(str(password or ""), None)             # the same cost whether or not the account exists
                raise Unauthenticated(WRONG)
            if row["locked_until"] and row["locked_until"] > datetime.now(UTC):
                outcome = "auth.locked"
            elif row["status"] != "active" or not check_password(str(password or ""), row["password_hash"]):
                failures = row["failures"] + 1
                lock = datetime.now(UTC) + timedelta(seconds=LOCK_S) if failures >= LOCK_AFTER else None
                await tx.run("UPDATE accounts SET failures = %s, locked_until = %s WHERE id = %s", failures, lock, row["id"])
                outcome = "auth.lock" if lock else "auth.fail"
            else:
                await tx.run("UPDATE accounts SET failures = 0, locked_until = NULL, signed_in_at = now() WHERE id = %s",
                             row["id"])
                signed, outcome = await self._sign_in(tx, row, client), "auth.login"
        await self._audit(row["id"], outcome, {"client": client} if outcome == "auth.login" else None)
        if outcome == "auth.locked":
            raise Locked("too many failed sign-ins; try again in a few minutes")
        if outcome != "auth.login":
            raise Unauthenticated(WRONG)
        return signed

    async def _sign_in(self, tx: Tx, row: dict[str, Any], client: str) -> dict[str, Any]:
        token = ids.new_secret("sign_in")
        expires = datetime.now(UTC) + timedelta(seconds=SIGN_IN_TTL_S)
        await tx.run("INSERT INTO sign_ins (id, account_id, digest, client, expires_at) VALUES (%s, %s, %s, %s, %s)",
                     ids.new("sign_in"), row["id"], ids.digest(token, self.pepper), client[:200], expires)
        memberships = await tx.all("SELECT org_id, role FROM memberships WHERE account_id = %s ORDER BY created_at", row["id"])
        return {"object": "sign_in", "token": token, "expires_at": ts(expires), "account": _view(row),
                "memberships": [{"org_id": m["org_id"], "role": m["role"]} for m in memberships]}

    async def logout(self, token: str) -> None:
        async with self.db.platform() as tx:
            await tx.run("UPDATE sign_ins SET revoked_at = now() WHERE digest = %s AND revoked_at IS NULL",
                         ids.digest(token, self.pepper))

    # ── SSO (SPEC §10.3): an identity provider vouched for a person ───────────────────────────────────────────────
    async def sso_sign_in(self, org_id: str, *, subject: str, email: str, name: str, role: str | None,
                          auto_provision: bool, default_role: str, client: str) -> dict[str, Any]:
        """The account the provider's claims name, provisioned when allowed, its membership's role updated when the
        claims say so; signed in. The provider's subject is remembered so a changed email cannot take over another
        account."""
        email = check_email(email)
        async with self.db.platform() as tx:
            row = await tx.one("SELECT * FROM accounts WHERE sso_subject = %s", f"{org_id}:{subject}") or \
                await tx.one("SELECT * FROM accounts WHERE email = %s AND sso_subject IS NULL", email)
            if row is None:
                if not auto_provision:
                    raise Unauthenticated("no account for this sign-in: ask an admin for an invitation")
                await tx.run("INSERT INTO accounts (id, email, name, status, sso_subject) VALUES (%s, %s, %s, 'active', %s)",
                             ids.new("account"), email, name[:200], f"{org_id}:{subject}")
                row = await tx.get("SELECT * FROM accounts WHERE email = %s", email)
                created = True
            else:
                created = False
                if row["status"] == "disabled":
                    raise Unauthenticated("the account is disabled")
                await tx.run("UPDATE accounts SET sso_subject = %s, status = 'active', name = CASE WHEN name = '' THEN %s "
                             "ELSE name END, failures = 0, locked_until = NULL, signed_in_at = now() WHERE id = %s",
                             f"{org_id}:{subject}", name[:200], row["id"])
            member = await tx.one("SELECT role FROM memberships WHERE account_id = %s AND org_id = %s", row["id"], org_id)
            if member is None:
                if not auto_provision:
                    raise Unauthenticated("no membership for this sign-in: ask an admin for an invitation")
                await tx.run("INSERT INTO memberships (account_id, org_id, role) VALUES (%s, %s, %s)",
                             row["id"], org_id, role or default_role)
            elif role is not None and role != member["role"]:
                if member["role"] == "admin" and await self._last_admin(tx, org_id, row["id"]):
                    raise Conflict("the organization keeps at least one active admin")
                await tx.run("UPDATE memberships SET role = %s WHERE account_id = %s AND org_id = %s", role, row["id"], org_id)
            row = await tx.get("SELECT * FROM accounts WHERE id = %s", row["id"])
            signed = await self._sign_in(tx, row, client)
        await self._audit(row["id"], "auth.sso", {"client": client, "provisioned": created})
        return signed

    @staticmethod
    async def _last_admin(tx: Tx, org_id: str, account_id: str) -> bool:
        return await tx.value("SELECT count(*) FROM memberships m JOIN accounts a ON a.id = m.account_id WHERE m.org_id = %s "
                              "AND m.role = 'admin' AND a.status <> 'disabled' AND a.id <> %s", org_id, account_id) == 0

    async def authenticate(self, secret: str) -> dict[str, Any] | None:
        """The account behind a live sign-in token and its memberships, or None."""
        if not secret.startswith(ids.SECRET["sign_in"]):
            return None
        async with self.db.platform() as tx:
            row = await tx.one("SELECT a.*, s.id AS sign_in_id FROM sign_ins s JOIN accounts a ON a.id = s.account_id "
                               "WHERE s.digest = %s AND s.revoked_at IS NULL AND s.expires_at > now() AND a.status = 'active'",
                               ids.digest(secret, self.pepper))
            if not row:
                return None
            if time.monotonic() - self._touched.get(row["sign_in_id"], 0) > TOUCH_S:
                self._touched[row["sign_in_id"]] = time.monotonic()
                await tx.run("UPDATE sign_ins SET last_used_at = now() WHERE id = %s", row["sign_in_id"])
            memberships = await tx.all("SELECT m.org_id, m.role FROM memberships m JOIN orgs o ON o.id = m.org_id "
                                       "WHERE m.account_id = %s AND o.status = 'active' ORDER BY m.created_at", row["id"])
        return {"account": _view(row), "memberships": [dict(m) for m in memberships]}
