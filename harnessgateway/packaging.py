"""A package on disk: packing a folder, laying a package out as an install finds it, and checking one (spec §8).

`check` runs the package's own `run` on this machine, against the cases under its `tests/` folder. It is for
the person writing the package; the platform itself only ever runs a package inside a sandbox.
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harnessgateway.domain import contract
from harnessgateway.domain.archive import KIT_DIR, Package, PackageFile, read_archive, write_archive
from harnessgateway.domain.events import LineError, RunOutput
from harnessgateway.domain.outcome import Terminal
from harnessgateway.domain.sealing import new_placeholder
from harnessgateway.errors import Invalid
from harnessgateway.kit import hgkit

CASE_TIMEOUT_S = 120
LEFT_OUT = frozenset({".git", "__pycache__", ".DS_Store"})        # never part of a package


def _members(folder: Path) -> Iterator[Path]:
    for path in sorted(folder.iterdir()):
        if path.name in LEFT_OUT:
            continue
        if path.is_symlink():
            raise Invalid(f"{path.name} is a link; a package holds plain files and folders only")
        if path.is_dir():
            yield from _members(path)
        elif path.is_file():
            yield path


def pack(folder: Path) -> bytes:
    """A folder as a package archive. The same files give the same bytes, whenever and wherever it is packed."""
    return write_archive(
        PackageFile(path.relative_to(folder).as_posix(), bool(path.stat().st_mode & 0o100), path.read_bytes())
        for path in _members(folder))


def lay_out(package: Package, target: Path) -> None:
    """Write a package's files into a folder, and the kit beside them: what `install` and `run` find at /harness."""
    files = [(file.path, file.executable, file.content) for file in package.files]
    for path, executable, content in [*files, (f"{KIT_DIR}/hgkit.py", False, Path(hgkit.__file__).read_bytes())]:
        location = target / path
        location.parent.mkdir(parents=True, exist_ok=True)
        location.write_bytes(content)
        location.chmod(0o755 if executable else 0o644)


@dataclass(frozen=True)
class CaseResult:
    name: str
    printed: tuple[dict[str, Any], ...]        # every line `run` printed
    terminal: Terminal
    output: list[dict[str, Any]]               # the response's output items


def _variables(package: Package, base: Path, handover: str, recording: Path | None) -> dict[str, str]:
    """The variables of spec §7.3, pointing at folders on this machine, with a stand-in provider."""
    manifest = package.manifest
    api = manifest.apis[0] if manifest.apis else None
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(base / "mind"),
        "HG_CONTRACT": str(contract.CONTRACT_VERSION),
        "HG_TURN": str(base / "turn"),
        "HG_MIND": str(base / "mind"),
        "HG_WORK": str(base / "work"),
        "HG_HARNESS": str(base / "harness"),
        "HG_STATE": str(base / "mind" / contract.STATE_DIR / manifest.name),
        "HG_SESSION": "sess_check",
        "HG_RESPONSE": "resp_check",
        "HG_HARNESS_NAME": manifest.name,
        "HG_HARNESS_VERSION": manifest.version,
        "HG_ENVIRONMENT": "check",
        "HG_PROVIDER": "check" if api else "",
        "HG_PROVIDER_API": api or "",
        "HG_PROVIDER_URL": "https://provider.invalid" if api else "",
        "HG_PROVIDER_KEY": new_placeholder() if api else "",
        "HG_MODEL": "check-model" if api else "",
        "HG_HANDOVER": handover,
        "HG_CHECK": "1",
        **({"HG_REPLAY": str(recording)} if recording else {}),
        **{name: setting.default for name, setting in manifest.settings.items() if setting.default is not None},
    }


def _optional(path: Path, default: str) -> str:
    return path.read_text() if path.exists() else default


def _run_case(package: Package, case: Path, base: Path) -> CaseResult:
    where = f"tests/{case.name}"
    for name in ("harness", "mind", "work", "turn", "turn/files"):
        (base / name).mkdir()
    lay_out(package, base / "harness")
    handover = _optional(case / "handover.txt", "").strip()
    (base / "turn" / "request.json").write_text((case / "request.json").read_text())
    (base / "turn" / "history.json").write_text(_optional(case / "history.json", "[]"))
    (base / "turn" / "handover.txt").write_text(handover)
    recording = case / "recording"
    variables = _variables(package, base, handover, recording if recording.exists() else None)
    try:
        # The package's own `run`, started by the person checking it.
        done = subprocess.run([str(base / "harness" / "run")], cwd=base / "work", env=variables,  # noqa: S603
                              capture_output=True, text=True, timeout=CASE_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        raise Invalid(f"{where}: run did not finish in {CASE_TIMEOUT_S} seconds") from None
    except OSError as e:
        raise Invalid(f"{where}: run could not be started: {e}") from None
    if done.returncode != 0:
        raise Invalid(f"{where}: run exited with code {done.returncode}: {done.stderr.strip()[-500:]}")
    reader = RunOutput(first_sequence_number=2)
    printed = []
    for number, text in enumerate(done.stdout.splitlines(), 1):
        try:
            if reader.read(text) is None:
                raise Invalid(f"{where}: line {number} comes after the terminal line")
        except LineError as e:
            raise Invalid(f"{where}: line {number}: {e}") from None
        printed.append(json.loads(text))
    if reader.terminal is None:
        raise Invalid(f"{where}: run printed no terminal line")
    return CaseResult(case.name, tuple(printed), reader.terminal, reader.output())


def _hold_to_expected(case: Path, result: CaseResult) -> None:
    expected = [json.loads(line) for line in (case / "expected.jsonl").read_text().splitlines() if line]
    for number, (want, got) in enumerate(zip(expected, result.printed, strict=False), 1):
        if want != got:
            raise Invalid(f"tests/{case.name}: line {number} is not the expected one\n"
                          f"  expected: {json.dumps(want)}\n  printed:  {json.dumps(got)}")
    if len(expected) != len(result.printed):
        raise Invalid(f"tests/{case.name}: {len(result.printed)} lines were printed, {len(expected)} were expected")


def check(folder: Path, *, record: bool = False) -> list[CaseResult]:
    """Validate a package folder and run each case under its tests/ (spec §8.9). Raises Invalid, naming what
    is wrong. With `record`, what each case printed is written as its expected.jsonl."""
    if not folder.is_dir():
        raise Invalid(f"{folder} is not a folder")
    folder = folder.resolve()                      # `run` starts in a folder of its own: name files in full
    package = read_archive(pack(folder))
    results = []
    tests = folder / "tests"
    for case in sorted(path.parent for path in tests.glob("*/request.json")) if tests.is_dir() else []:
        with tempfile.TemporaryDirectory(prefix="hg-check-") as base:
            result = _run_case(package, case, Path(base))
        if record:
            (case / "expected.jsonl").write_text("".join(json.dumps(line) + "\n" for line in result.printed))
        elif (case / "expected.jsonl").exists():
            _hold_to_expected(case, result)
        results.append(result)
    return results
