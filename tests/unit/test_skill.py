import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "harness-package-builder"


def test_skill_ships_the_same_hgkit_as_the_sandbox():
    assert (SKILL / "scripts" / "hgkit" / "__init__.py").read_bytes() == (ROOT / "sandbox" / "hgkit" / "__init__.py").read_bytes()


def test_skill_example_runs_against_a_recorded_provider_stream():
    example = SKILL / "examples" / "minimal"
    r = subprocess.run([sys.executable, str(SKILL / "scripts" / "validate.py"), str(example), "--prompt", "hi",
                        "--provider-fixture", str(example / "tests" / "fixture-anthropic.sse"), "--model", "m"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "PASS" in r.stdout, r.stdout + r.stderr


def test_skill_frontmatter():
    head = (SKILL / "SKILL.md").read_text().split("---")[1]
    assert "name: harness-package-builder" in head and "description:" in head
