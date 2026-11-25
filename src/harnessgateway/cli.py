"""The command line: `serve` (the platform), `proxy` (the egress proxy), `init` (the first-time setup), and what
installs a released image: `compose` (how it is started) and `secrets` (a new installation's settings)."""

import argparse
import asyncio
import base64
import logging
import os
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path

from harnessgateway import __version__
from harnessgateway.settings import ProxySettings, Settings, SettingsError

COMPOSE = Path(__file__).parent / "compose.yaml"   # how a released image is run: beside the program, in every image
_BUILT_HERE = "${HG_IMAGE:-harnessgateway:dev}"    # what that file runs when nothing says which image


def compose(env: Mapping[str, str]) -> str:
    """The Compose file this version is run with, naming this image: what HG_IMAGE says the image is, which an
    image is told when it is built. An installation may still name another in its .env (a copy in a registry of
    its own)."""
    image = env.get("HG_IMAGE")
    if not image:
        raise SettingsError("HG_IMAGE is not set: this is not a released image. A checkout is run with "
                            "compose.dev.yaml")
    text = COMPOSE.read_text()
    assert text.count(_BUILT_HERE) == 1, "the Compose file names its image once"
    return text.replace(_BUILT_HERE, f"${{HG_IMAGE:-{image}}}")


def new_secrets() -> str:
    """A new installation's .env: its three secrets, and the defaults a person may change."""
    def secret(size: int) -> str:
        return secrets.token_urlsafe(size)
    return (f"HG_SECRET_KEY={base64.b64encode(secrets.token_bytes(32)).decode()}\n"
            f"HG_PROXY_KEY={secret(32)}\nHG_DB_PASSWORD={secret(24)}\n"
            "HG_PORT=8000\nHG_BIND=127.0.0.1\nHG_NAMESPACE=hg\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harnessgateway", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the API and the turn runner")
    commands.add_parser("proxy", help="run the egress proxy")
    init = commands.add_parser("init", help="set the installation up: its defaults, and its first API key, printed")
    init.add_argument("--new-key", action="store_true", help="make another key even though keys exist")
    commands.add_parser("compose", help="print the Compose file this version is run with")
    commands.add_parser("secrets", help="print a new installation's .env: run once, and keep what it prints")
    commands.add_parser("schema", help="write the Responses schemas the pinned OpenAI SDK gives, into the contract")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("HG_LOG_LEVEL", "info").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)     # it logs every request, and drowns the platform's own
    try:
        if args.command == "compose":
            print(compose(os.environ), end="")
            return 0
        if args.command == "secrets":
            print(new_secrets(), end="")
            return 0
        if args.command == "schema":               # for whoever changes the pin: nothing of an installation is read
            from harnessgateway import shipped
            from harnessgateway.domain import schema
            changed = schema.write(shipped.contract())
            print(f"{schema.SDK}: " + (f"wrote {', '.join(changed)}" if changed else "the schemas are up to date"))
            return 0
        if args.command == "proxy":                # imported where it runs: the proxy never loads the platform
            from harnessgateway.proxy.control import run_proxy
            asyncio.run(run_proxy(ProxySettings.from_env(os.environ)))
            return 0
        from harnessgateway.serve import Broken, serve
        from harnessgateway.serve import init as make_key
        from harnessgateway.store.db import NewerDatabase
        settings = Settings.from_env(os.environ)
        try:
            if args.command == "init":
                key = asyncio.run(make_key(settings, new_key=args.new_key))
                print(f"API key (shown once): {key}" if key
                      else "Already set up: the API key was printed the first time.")
                return 0
            asyncio.run(serve(settings))
        except (Broken, NewerDatabase) as broken:
            print(f"harnessgateway: {broken}", file=sys.stderr)
            return 1
        return 0
    except SettingsError as wrong:
        print(f"harnessgateway: {wrong}", file=sys.stderr)
        return 2
