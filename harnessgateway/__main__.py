"""The `harnessgateway` command (SPEC §10.1).

    harnessgateway serve --role api,gateway     the API and/or the provider gateway, in this process
    harnessgateway node                         a node: runs sandboxes on this host's Docker with gVisor
    harnessgateway door                         a sandbox's door (started by the node)
    harnessgateway migrate                      apply the schema migrations
    harnessgateway init                         the self-hosted organization, its built-ins and first admin key (once)
    harnessgateway node-token create            a join token for nodes of a pool
    harnessgateway keygen                       fresh values for HG_SECRET_KEY, HG_OPERATOR_KEY, HG_NODE_JOIN_TOKEN

Every setting comes from the environment (HG_*); see docs/operations.md.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
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
    for noisy in ("httpx", "uvicorn.access", "botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _until_signal(*tasks: asyncio.Task[Any]) -> None:
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    stopper = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
    for t in done:
        if t is not stopper and not t.cancelled() and t.exception():
            raise t.exception()  # type: ignore[misc]


async def serve(roles: tuple[str, ...]) -> None:
    import uvicorn

    from .services.platform import Platform
    settings = Settings.load(roles)
    setup_logging(settings.log_level)
    plat = Platform(settings)
    if "api" in roles:
        from .api.app import create_api
        await plat.db.open()
        try:
            await plat.migrate()
        finally:
            await plat.db.close()
        plat = Platform(settings)
        app = create_api(plat)
        if "gateway" in roles:
            from .gateway.app import Gateway, gateway_router
            gw = Gateway(plat)
            app.include_router(gateway_router(gw))
    else:
        from .gateway.app import create_gateway
        app = create_gateway(plat)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_config=None,
                                           proxy_headers=True, server_header=False, timeout_keep_alive=75))
    logging.getLogger("harnessgateway").info("serving %s on %s:%s", ",".join(roles), settings.host, settings.port)
    await server.serve()


async def run_node() -> None:
    from .node.agent import Node
    settings = Settings.load(("node",))
    setup_logging(settings.log_level)
    node = asyncio.create_task(Node(settings).run())
    await _until_signal(node)
    node.cancel()                                  # its own shutdown runs before the loop cancels everything else
    with contextlib.suppress(asyncio.CancelledError):
        await node


async def admin(args: argparse.Namespace) -> None:
    from .services.platform import Platform
    setup_logging("warning")
    plat = Platform(Settings.load(("api",)))
    await plat.db.open()
    try:
        await plat.migrate()
        if args.command == "migrate":
            print(json.dumps({"ok": True}))
        elif args.command == "init":
            org, key = await plat.init(args.name)
            print(json.dumps({"organization": org["id"], "name": org["name"], "admin_key": key,
                              "note": None if key else "already initialized: no new key"}, indent=2))
        elif args.command == "node-token":
            token = await plat.scheduler.create_token(args.pool, args.label, args.uses, args.ttl_days)
            print(json.dumps({"token": token, "pool": args.pool}, indent=2))
    finally:
        await plat.db.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="harnessgateway", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("serve").add_argument("--role", default="api,gateway")
    sub.add_parser("node")
    sub.add_parser("door", add_help=False)
    sub.add_parser("migrate")
    sub.add_parser("keygen")
    sub.add_parser("init").add_argument("--name", default="default")
    p = sub.add_parser("node-token")
    p.add_argument("action", choices=["create"])
    p.add_argument("--pool", default="")
    p.add_argument("--label", default="")
    p.add_argument("--uses", type=int, default=1)
    p.add_argument("--ttl-days", type=int, default=7)
    args, rest = ap.parse_known_args(argv)
    if args.command == "door":
        from .node import door
        door.run(rest)
    elif rest:
        ap.error(f"unrecognized arguments: {' '.join(rest)}")
    elif args.command == "keygen":
        print(f"HG_SECRET_KEY={base64.b64encode(secrets.token_bytes(32)).decode()}\n"
              f"HG_OPERATOR_KEY=hgo_{secrets.token_urlsafe(32)}\nHG_NODE_JOIN_TOKEN=hgj_{secrets.token_urlsafe(32)}")
    elif args.command == "serve":
        roles = tuple(sorted({r.strip() for r in args.role.split(",") if r.strip()}))
        if not roles or set(roles) - {"api", "gateway"}:
            ap.error("--role is api, gateway or api,gateway (a node runs with `harnessgateway node`)")
        asyncio.run(serve(roles))
    elif args.command == "node":
        asyncio.run(run_node())
    else:
        asyncio.run(admin(args))


if __name__ == "__main__":
    main()
