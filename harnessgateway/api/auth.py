"""Layer 6: who is asking. A Bearer secret is a member's API key, the deployer's key, or a worker's
credential; each route says which it takes. The app signs in with an account instead, and its
session cookie names the member the account is in one org. Nothing else logs in."""
from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from ..accounts import Account, Accounts
from ..store import Member, Store, Worker

DEPLOYER = Member("", "", "deployer", 0)
COOKIE = "hg_session"


@dataclass(frozen=True)
class Principals:
    store: Store
    deployer_key: str = ""                           # from the environment; wins over the stored one
    join_token: str = ""
    accounts: Accounts | None = None                 # the app's sessions, when accounts are served

    @staticmethod
    def bearer(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, "a Bearer API key is required")
        return auth[7:].strip()

    def is_deployer(self, secret: str) -> bool:
        if self.deployer_key and hmac.compare_digest(secret, self.deployer_key):
            return True
        return self.store.is_deployer(secret)

    def session(self, request: Request) -> tuple[Account, Member] | None:
        """The account and member behind the app's cookie, if there is one and it still stands."""
        token = request.cookies.get(COOKIE, "")
        return self.accounts.resolve_session(token) if self.accounts and token else None

    def account(self, request: Request) -> tuple[Account, Member]:
        found = self.session(request)
        if found is None:
            raise HTTPException(401, "sign in")
        return found

    def member(self, request: Request) -> Member:
        if not request.headers.get("authorization"):
            found = self.session(request)
            if found is None:
                raise HTTPException(401, "a Bearer API key is required, or the app's sign-in")
            request.state.account = found[0]
            return found[1]
        secret = self.bearer(request)
        if self.is_deployer(secret):
            raise HTTPException(403, "the deployer key manages organizations; a member's key does this")
        who = self.store.resolve_key(secret)
        if who is None:
            raise HTTPException(401, "unknown or revoked API key")
        return who

    def owner(self, request: Request) -> Member:
        who = self.member(request)
        if not who.owner:
            raise HTTPException(403, "an owner's key does this")
        return who

    def deployer(self, request: Request) -> Member:
        if not self.is_deployer(self.bearer(request)):
            raise HTTPException(403, "the deployer key does this")
        return DEPLOYER

    def joiner(self, request: Request) -> None:
        if not self.join_token or not hmac.compare_digest(self.bearer(request), self.join_token):
            raise HTTPException(403, "a worker joins with the join token")

    def worker(self, request: Request) -> Worker:
        w = self.store.worker_by_secret(self.bearer(request))
        if w is None:
            raise HTTPException(401, "unknown worker credential")
        return w

    def deps(self):
        """FastAPI dependencies for each principal."""
        return (Depends(self.member), Depends(self.owner), Depends(self.deployer), Depends(self.joiner), Depends(self.worker))
