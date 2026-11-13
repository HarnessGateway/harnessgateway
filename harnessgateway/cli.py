"""The program's commands (spec §18.1)."""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import harnessgateway
from harnessgateway import packaging
from harnessgateway.domain.archive import read_archive
from harnessgateway.errors import Refused
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


def _step(args: argparse.Namespace) -> int:
    return asyncio.run(run_step(args.kind, args.target, limit_mb=args.limit_mb, env=os.environ))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harnessgateway", description=harnessgateway.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="print the version").set_defaults(run=_version)

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
