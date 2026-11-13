"""The program's commands (spec §18.1)."""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import harnessgateway
from harnessgateway import logs, packaging, serve
from harnessgateway.domain.archive import read_archive
from harnessgateway.errors import Refused
from harnessgateway.proxy.command import run_proxy
from harnessgateway.settings import ProxySettings, Settings, SettingsError
from harnessgateway.steps.command import KINDS, run_step


def _version(_: argparse.Namespace) -> int:
    print(harnessgateway.__version__)
    return 0


def _package_check(args: argparse.Namespace) -> int:
    folder = Path(args.folder)
    try:
        results = packaging.check(folder, record=args.record)
        package = read_archive(packaging.pack(folder))
    except Refused as e:
        print(e.message, file=sys.stderr)
        return 1
    print(f"{package.manifest.name} {package.manifest.version}  {len(package.files)} files  {package.digest[:12]}")
    for result in results:
        print(f"tests/{result.name}: {'recorded' if args.record else 'passed'}")
    return 0


def _settings() -> Settings | None:
    try:
        return Settings.from_env(os.environ)
    except SettingsError as e:
        print(f"settings: {e}", file=sys.stderr)
        return None


def _serve(_: argparse.Namespace) -> int:
    settings = _settings()
    if settings is None:
        return 2
    logs.setup(settings.log_level)
    try:
        asyncio.run(serve.serve(settings))
    except SettingsError as e:
        print(f"settings: {e}", file=sys.stderr)
        return 2
    return 0


def _proxy(_: argparse.Namespace) -> int:
    try:
        settings = ProxySettings.from_env(os.environ)
    except SettingsError as e:
        print(f"settings: {e}", file=sys.stderr)
        return 2
    logs.setup(settings.log_level)
    asyncio.run(run_proxy(settings))
    return 0


def _init(_: argparse.Namespace) -> int:
    settings = _settings()
    if settings is None:
        return 2
    key = asyncio.run(serve.init(settings))
    if key is None:
        print("Already set up: the organization, its default environment and a key exist. Nothing was changed.")
    else:
        print(f"The organization and its default environment are ready.\nAdmin key (shown once): {key}")
    return 0


def _step(args: argparse.Namespace) -> int:
    return asyncio.run(run_step(args.kind, args.target, limit_mb=args.limit_mb, env=os.environ))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harnessgateway", description=harnessgateway.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="print the version").set_defaults(run=_version)
    commands.add_parser("serve", help="run the APIs, the turn runner and housekeeping").set_defaults(run=_serve)
    commands.add_parser("proxy", help="run the egress proxy").set_defaults(run=_proxy)
    commands.add_parser("init", help="first-time setup: the organization, the default environment, an admin key"
                        ).set_defaults(run=_init)

    package = commands.add_parser("package", help="work on a harness package").add_subparsers(
        dest="package_command", required=True)
    check = package.add_parser("check", help="validate a package folder and run its tests")
    check.add_argument("folder")
    check.add_argument("--record", action="store_true",
                       help="write what each test printed as its expected.jsonl, instead of comparing")
    check.set_defaults(run=_package_check)

    step = commands.add_parser("step", help="run one trusted step of a turn or an install (the platform does this)")
    step.add_argument("kind", choices=KINDS)
    step.add_argument("target", help="the turn's id, or the install's")
    step.add_argument("--limit-mb", type=int, default=0, help="watch: the disk limit")
    step.set_defaults(run=_step)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result: int = args.run(args)
    return result
