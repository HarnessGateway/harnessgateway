"""The command line: `serve` (the platform), `proxy` (the egress proxy) and `init` (the first-time setup)."""

import argparse
import asyncio
import logging
import os
import sys

from harnessgateway import __version__
from harnessgateway.settings import ProxySettings, Settings, SettingsError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harnessgateway", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the API and the turn runner")
    commands.add_parser("proxy", help="run the egress proxy")
    init = commands.add_parser("init", help="set the installation up: its defaults, and its first API key, printed")
    init.add_argument("--new-key", action="store_true", help="make another key even though keys exist")
    commands.add_parser("schema", help="write the Responses schemas the pinned OpenAI SDK gives, into the contract")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("HG_LOG_LEVEL", "info").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)     # it logs every request, and drowns the platform's own
    try:
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
        settings = Settings.from_env(os.environ)
        if args.command == "init":
            key = asyncio.run(make_key(settings, new_key=args.new_key))
            print(f"API key (shown once): {key}" if key else "Already set up: the API key was printed the first time.")
            return 0
        try:
            asyncio.run(serve(settings))
        except Broken as broken:
            print(f"harnessgateway: {broken}", file=sys.stderr)
            return 1
        return 0
    except SettingsError as wrong:
        print(f"harnessgateway: {wrong}", file=sys.stderr)
        return 2
