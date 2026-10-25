"""The `harnessgateway` command.

    harnessgateway serve [--role api,gateway,node]   run one or more roles in this process (all three by default)
    harnessgateway migrate                           apply database migrations, then exit
    harnessgateway keygen                            print fresh values for HG_SECRET_KEY and HG_OPERATOR_KEY
    harnessgateway create-org NAME EMAIL             an organization with its owner's admin key (printed once)
    harnessgateway invite [--uses N] [--label L]     an invite code for the pilot's sign-up (printed once)
    harnessgateway node-token [--org ORG_ID]         a join token for a node (shared pool, or dedicated to one org)

Every setting comes from the environment (HG_*); see docs/operations.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import signal
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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


async def serve(settings: Settings) -> None:
    import uvicorn

    servers, tasks = [], []
    if {"api", "gateway"} & set(settings.roles):
        from .services.platform import Platform
        if "api" in settings.roles:
            from .api.app import create_app
            plat = Platform(settings)
            await plat.db.migrate()
            servers.append(uvicorn.Server(uvicorn.Config(create_app(plat), host=settings.host, port=settings.port,
                                                         log_config=None, proxy_headers=True, server_header=False)))
        if "gateway" in settings.roles:
            from .gateway.app import create_gateway
            servers.append(uvicorn.Server(uvicorn.Config(create_gateway(Platform(settings)), host=settings.gateway_host,
                                                         port=settings.gateway_port, log_config=None, server_header=False)))
    for server in servers:
        server.install_signal_handlers = lambda: None   # type: ignore[method-assign]
        tasks.append(asyncio.create_task(server.serve()))
    node_task = None
    if "node" in settings.roles:
        from .node.agent import Node
        node_task = asyncio.create_task(Node(settings).run())
        tasks.append(node_task)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    stopper = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
    for server in servers:                           # servers drain their requests; the node stops at once
        server.should_exit = True
    if node_task:
        node_task.cancel()
    await asyncio.wait(tasks, timeout=30)
    for t in done:
        if t is not stopper and not t.cancelled() and t.exception():
            raise t.exception()                      # type: ignore[misc]


async def admin(command: str, args: argparse.Namespace) -> None:
    from .services.platform import Platform
    plat = Platform(Settings.from_env(("api",)))
    await plat.open()
    try:
        if command == "migrate":
            print(json.dumps({"applied": await plat.db.migrate()}))
            return
        await plat.packages.publish_builtins()
        if command == "create-org":
            org, key = await plat.create_org(args.name, args.email, None, "cli")
            print(json.dumps({"organization": org, "api_key": key}, indent=2))
        elif command == "invite":
            print(json.dumps(await plat.tenancy.create_invite(args.label, args.uses, args.ttl_days, None, "cli"), indent=2))
        elif command == "node-token":
            print(json.dumps(await plat.scheduler.create_join_token(args.org, args.label, args.uses, args.ttl_days, "cli"),
                             indent=2))
    finally:
        await plat.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="harnessgateway", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--role", default="api,gateway,node")
    sub.add_parser("migrate")
    sub.add_parser("keygen")
    p = sub.add_parser("create-org")
    p.add_argument("name")
    p.add_argument("email")
    p = sub.add_parser("invite")
    p.add_argument("--uses", type=int, default=1)
    p.add_argument("--label", default="")
    p.add_argument("--ttl-days", type=int, default=30)
    p = sub.add_parser("node-token")
    p.add_argument("--org", default=None)
    p.add_argument("--label", default="")
    p.add_argument("--uses", type=int, default=1)
    p.add_argument("--ttl-days", type=int, default=7)
    args = ap.parse_args(argv)
    if args.command == "keygen":
        print(f"HG_SECRET_KEY={secrets.token_hex(32)}\nHG_OPERATOR_KEY=hgo_{secrets.token_urlsafe(32)}\n"
              f"HG_NODE_JOIN_TOKEN=hgj_{secrets.token_urlsafe(32)}")
        return
    if args.command == "serve":
        settings = Settings.from_env(tuple(r.strip() for r in args.role.split(",") if r.strip()))
        setup_logging(settings.log_level)
        asyncio.run(serve(settings))
        return
    setup_logging("warning")
    asyncio.run(admin(args.command, args))


if __name__ == "__main__":
    main()
