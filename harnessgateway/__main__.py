"""The `harnessgateway` command (SPEC §6, §15).

    harnessgateway api                          the API: callers, boxes, doors and the console talk to it
    harnessgateway worker                       jobs (launch, install, stop, mail, delete_session) and duties
    harnessgateway door                         the egress proxy boxes reach the network through
    harnessgateway migrate                      apply the schema migrations
    harnessgateway init --admin-email EMAIL     the self-hosted organization, its first admin and an admin key (once)
    harnessgateway keygen                       fresh values for HG_SECRET_KEY, HG_OPERATOR_KEY, HG_DOOR_KEY

Every setting comes from the environment (HG_*); see docs/operations.md.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import secrets
import sys
from typing import Any

from . import __version__
from .config import Settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {"ts": round(record.created, 3), "level": record.levelname.lower(), "logger": record.name,
                               "message": record.getMessage()}
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    for noisy in ("httpx", "uvicorn.access", "botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def api() -> None:
    import uvicorn

    from .api.app import create_app
    from .services.platform import Platform
    settings = Settings.load(("api",))
    setup_logging(settings.log_level)
    plat = Platform(settings)
    await plat.open()
    try:
        await plat.migrate()
    finally:
        await plat.close()
    plat = Platform(settings)
    server = uvicorn.Server(uvicorn.Config(create_app(plat), host=settings.host, port=settings.port, log_config=None,
                                           proxy_headers=True, server_header=False, timeout_keep_alive=75))
    logging.getLogger("harnessgateway").info("api %s on %s:%s", __version__, settings.host, settings.port)
    await server.serve()


async def worker() -> None:
    from .services.platform import Platform
    from .worker.main import Worker
    settings = Settings.load(("worker",))
    setup_logging(settings.log_level)
    await Worker(Platform(settings)).run()


def door() -> None:
    from .door.main import run
    settings = Settings.load(("door",))
    setup_logging(settings.log_level)
    run(settings)


async def admin(args: argparse.Namespace) -> None:
    from .services.platform import Platform
    setup_logging("warning")
    plat = Platform(Settings.load(("api",)))
    await plat.open()
    try:
        await plat.migrate()
        if args.command == "migrate":
            print(json.dumps({"ok": True}))
        else:
            out = await plat.init(args.name, args.admin_email)
            doc = {"organization": out["organization"]["id"], "name": out["organization"]["name"]}
            if "admin" in out:
                doc |= {"admin_email": out["admin"]["email"], "invitation": out["admin"]["invitation"],
                        "invitation_sent": out["admin"]["sent"], "admin_key": out["admin_key"]}
            else:
                doc["note"] = out["note"]
            print(json.dumps(doc, indent=2))
    finally:
        await plat.close()


def keygen() -> str:
    return (f"HG_SECRET_KEY={base64.b64encode(secrets.token_bytes(32)).decode()}\n"
            f"HG_OPERATOR_KEY=hgo_{secrets.token_urlsafe(32)}\nHG_DOOR_KEY=hgd_{secrets.token_urlsafe(32)}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="harnessgateway", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("api", "worker", "door", "migrate", "keygen"):
        sub.add_parser(name)
    p = sub.add_parser("init")
    p.add_argument("--name", default="default")
    p.add_argument("--admin-email", default=os.environ.get("HG_ADMIN_EMAIL", ""),
                   help="the first admin's login (or HG_ADMIN_EMAIL); the invitation that activates it is printed once")
    args = ap.parse_args(argv)
    if args.command == "keygen":
        print(keygen())
    elif args.command == "api":
        asyncio.run(api())
    elif args.command == "worker":
        asyncio.run(worker())
    elif args.command == "door":
        door()
    else:
        asyncio.run(admin(args))


if __name__ == "__main__":
    main()
