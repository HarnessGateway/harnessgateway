"""The security page's promises each name their proof; every proof names a test that exists. A promise
whose test is renamed or removed fails here, so the table and the suite cannot drift apart."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def proofs() -> list[tuple[str, str]]:
    rows = [line for line in (ROOT / "docs" / "security.md").read_text().splitlines() if re.match(r"\| \d+ \|", line)]
    assert len(rows) >= 13
    out = []
    for row in rows:
        proof = row.split("|")[3]
        for ref in re.findall(r"`([a-z_/]+\.py)(?:::([a-z_]+))?`", proof):
            out.append(ref)
    return out


def test_every_promise_has_a_test_that_exists():
    found = proofs()
    assert found
    for file, fn in found:
        candidates = [TESTS / file, TESTS / "conformance" / Path(file).name]
        path = next((c for c in candidates if c.is_file()), None)
        assert path is not None, f"{file} named by docs/security.md does not exist"
        if fn:
            assert f"def {fn}(" in path.read_text(), f"{file}::{fn} named by docs/security.md does not exist"


def test_the_numbered_promises_are_in_order_and_complete():
    numbers = [int(line.split("|")[1]) for line in (ROOT / "docs" / "security.md").read_text().splitlines() if re.match(r"\| \d+ \|", line)]
    assert sorted(numbers) == list(range(1, len(numbers) + 1)), numbers
