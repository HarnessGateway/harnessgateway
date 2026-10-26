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


def test_built_in_packages_name_no_cpu():
    """Every built-in package installs on x86_64 and ARM64 workers: npm or pip picks the build, never the package."""
    for pkg in sorted((ROOT / "packages").iterdir()):
        if (pkg / "harness.json").is_file():
            r = subprocess.run([sys.executable, str(SKILL / "scripts" / "validate.py"), str(pkg)],
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 0 and "no CPU named" in r.stdout, f"{pkg.name}: {r.stdout}{r.stderr}"


def test_validator_refuses_a_script_that_names_a_cpu(tmp_path):
    example = SKILL / "examples" / "minimal"
    pkg = tmp_path / "pkg"
    subprocess.run(["cp", "-r", str(example), str(pkg)], check=True)
    (pkg / "install.sh").write_text((pkg / "install.sh").read_text() + 'curl -o x "https://example.com/cli-linux-x64"\n')
    r = subprocess.run([sys.executable, str(SKILL / "scripts" / "validate.py"), str(pkg)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 1 and "names a CPU" in r.stdout and "install.sh:" in r.stdout
